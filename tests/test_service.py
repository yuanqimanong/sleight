"""Same transactional service behavior on SQLite and a disposable PostgreSQL DB."""

import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from sleight.client import ServiceClient
from sleight.core import errors
from sleight.core.types import DomReady
from sleight.deploy.api.app import create_app
from sleight.lease.database import DatabaseLease
from sleight.service.identity import Identity, current


@pytest.fixture
def db(service_db):
    return service_db


def test_lease_expiration_requires_confirmed_cleanup(db):
    lease = DatabaseLease(db)
    token = lease.acquire("env:instance", ttl=.01)
    time.sleep(.02)
    assert not lease.renew("env:instance", token, ttl=10)
    assert lease.acquire("env:instance", ttl=10) is None
    lease.release("env:instance", "wrong-token")
    assert lease.acquire("env:instance", ttl=10) is None
    lease.cleaned("instance")
    assert lease.acquire("env:instance", ttl=10)


def test_global_last_slot_and_cleanup_failure(db):
    from sleight.core.types import InstanceStatus
    from sleight.runtime import Governor, SQLiteLedger
    class Manager:
        def __init__(self):
            self.rows = {}
            self.fail = False
        def list_profiles(self):
            return list(self.rows.values())
        def status(self, pid):
            return InstanceStatus.RUNNING if pid in self.rows else InstanceStatus.NOT_FOUND
        def delete_profile(self, pid, *, force):
            if self.fail:
                raise errors.InstanceError("fixture outage")
            self.rows.pop(pid, None)
    ledger = SQLiteLedger(db=db)
    manager = Manager()
    governors = [Governor(resource="last-slot", limit=1, ledger=ledger, heartbeat=False) for _ in range(8)]
    def reserve(index):
        try:
            governors[index].created(str(index), ephemeral=True)
            governors[index].reserve(manager, str(index))
            return index
        except errors.Busy:
            return None
    with ThreadPoolExecutor(max_workers=8) as workers:
        chosen = [v for v in workers.map(reserve, range(8)) if v is not None]
    assert len(chosen) == 1
    pid = str(chosen[0])
    manager.rows[pid] = {"id": pid, "status": "running", "tags": []}
    with ledger.transaction("last-slot") as state:
        state["owners"] = {key: 0 for key in state["owners"]}
    manager.fail = True
    governors[0].collect(manager)
    assert governors[0].snapshot()["records"][pid]["slot"]
    manager.fail = False
    with ledger.transaction("last-slot") as state:
        for row in state["records"].values():
            row["retry_at"] = 0
    governors[0].collect(manager)
    assert not governors[0].snapshot()["records"]


def test_user_token_parent_revocation_and_secrets(db):
    identity = Identity()
    identity.ensure_admin("root-token")
    root = identity.lookup("root-token")
    delegated = identity.delegate(root, "pyp:alice", "Alice", 900)
    assert identity.lookup(delegated["token"]).user_id != "admin"
    assert identity.delegate(root, "pyp:alice", "Alice", 900)["user_id"] == delegated["user_id"]
    identity.revoke(root.token_id)
    assert identity.lookup(delegated["token"]) is None
    assert not identity.valid_id(delegated["id"])
    from sleight.service.identity import Vault
    Vault().put("proxy", "socks5://account:password@example.invalid:1080")
    assert "password" not in db.get("secrets", "proxy")
    assert "password" in Vault().get("proxy")


def test_delegate_only_token_cannot_manage_browsers(db):
    identity = Identity()
    identity.ensure_admin("root-token")
    token = identity.issue("admin", scopes=["delegate"])["token"]
    with TestClient(create_app(token="root-token", runtime=False)) as client:
        headers = {"X-Sleight-Token": token}
        assert client.get("/api/v1/profiles", headers=headers).status_code == 403
        assert client.get("/api/v1/tokens", headers=headers).status_code == 403
        assert client.post("/api/v1/delegate", headers=headers, json={"external_id": "pyp:admin"}).status_code == 200


class FakeSession:
    def __init__(self):
        self.target_id = "my-page"
        self.cdp_session_id = "cdp-page"
        self.closed = False
        self.transport = SimpleNamespace(close=lambda: None, call=lambda *a, **kw: {"product": "fixture"})
        self.counter = 0

    def title(self):
        self.counter += 1
        time.sleep(.005)
        return str(self.counter)

    def open(self, url, *, wait=None):
        assert isinstance(wait, DomReady)
        self.address = url

    def url(self):
        return self.address

    def eval(self, expr):
        return expr


@contextmanager
def fake_factory(profile, options):
    yield FakeSession()


class ClientTransport(httpx.BaseTransport):
    def __init__(self, client):
        self.client = client

    def handle_request(self, request):
        reply = self.client.request(request.method, request.url.path, content=request.content, headers=dict(request.headers))
        return httpx.Response(reply.status_code, headers=reply.headers, content=reply.content, request=request)


def test_sdk_serial_rpc_identity_and_mcp(db):
    app = create_app(token="root-token", runtime=False)
    identity = Identity()
    user = identity.save_user({"name": "Crawler", "limit": 1})
    issued = identity.issue(user["id"])
    other = identity.save_user({"name": "Other"})
    other_token = identity.issue(other["id"])
    pid = "fixture-profile"
    db.put("profiles", pid, {"id": pid, "owner": user["id"], "environment": "native", "source_id": "source",
                             "name": "fixture", "ephemeral": False, "state": "idle", "touched": time.time()})
    app.state.supervisor.factory = fake_factory
    with TestClient(app) as web, ServiceClient("http://testserver", issued["token"], transport=ClientTransport(web)) as client:
        with client.session(pid) as session:
            session.open("https://fixture.invalid", wait=DomReady())
            assert session.url() == "https://fixture.invalid"
            assert session.transport.call("Browser.getVersion")["product"] == "fixture"
            with ThreadPoolExecutor(max_workers=5) as pool:
                assert sorted(int(v) for v in pool.map(lambda _: session.title(), range(5))) == [1, 2, 3, 4, 5]
            with pytest.raises(errors.Busy):
                client.session(pid)
            assert web.get("/api/v1/profiles/" + pid, headers={"X-Sleight-Token": other_token["token"]}).status_code == 404
            assert web.get("/api/hosts", headers={"X-Sleight-Token": issued["token"]}).status_code == 403
            response = web.post("/mcp", headers={"X-Sleight-Token": issued["token"], "Accept": "application/json, text/event-stream"},
                json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}})
            assert response.status_code == 200, response.text
            assert response.json()["result"]["serverInfo"]["name"] == "sleight"
            response = web.post("/mcp", headers={"X-Sleight-Token": issued["token"], "X-Sleight-Session": session.id,
                    "Accept": "application/json, text/event-stream"}, json={"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                    "params": {"name": "browser_session", "arguments": {"action": "info"}}})
            assert response.status_code == 200, response.text
            assert not response.json()["result"].get("isError"), response.text
            identity.revoke(issued["id"])
            assert web.get("/api/auth/me", headers={"X-Sleight-Token": issued["token"]}).status_code == 401
            app.state.supervisor.sweep()
            app.state.supervisor.actors[session.id].done.wait(2)
        assert app.state.supervisor.get(session.id, identity.lookup("root-token"))["state"] == "released"


def test_templates_are_owner_scoped(db):
    from sleight.runtime.config import Templates
    identity = Identity()
    first = identity.save_user({"name": "First"})
    second = identity.save_user({"name": "Second"})
    a = identity.lookup(identity.issue(first["id"])["token"])
    b = identity.lookup(identity.issue(second["id"])["token"])
    ctx = current.set(a)
    try:
        row = Templates().save({"name": "Private", "proxy": "http://u:p@proxy.invalid:9000"})
        current.set(b)
        assert Templates().list() == []
        with pytest.raises(ValueError):
            Templates().apply({"template_id": row["id"]})
    finally:
        current.reset(ctx)
