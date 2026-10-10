"""Recovery, portable backups and credential boundaries on both storage backends."""

import json
import sqlite3
import time
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from sleight.core import errors
from sleight.deploy.api.app import create_app
from sleight.lease.database import DatabaseLease
from sleight.service.backup import export_data, import_data
from sleight.service.database import Database, metadata
from sleight.service.execution import Supervisor
from sleight.service.identity import Identity, Vault


def test_portable_backup_round_trip_and_refuse_overwrite(service_db, tmp_path):
    db = service_db
    identity = Identity()
    identity.ensure_admin("root")
    user = identity.save_user({"name": "fin", "default_environment": "local/default"})
    token = identity.issue(user["id"])
    Vault().put("proxy", "http://user:password@fixture.invalid:80")
    db.put("profiles", "fixed", {"owner": user["id"], "name": "fixed", "ephemeral": False})
    target = tmp_path / "portable.json"
    export_data(target, db=db)
    assert "password" not in target.read_text(encoding="utf-8")
    with pytest.raises(FileExistsError):
        export_data(target, db=db)
    with pytest.raises(ValueError, match="空数据库"):
        import_data(target, db=db)
    with db.transaction() as conn:
        for table in reversed(metadata.sorted_tables):
            conn.execute(table.delete())
    assert import_data(target, db=db)["ok"]
    assert identity.lookup(token["token"]).user_id == user["id"]
    assert Vault().get("proxy") == "http://user:password@fixture.invalid:80"
    assert db.get("profiles", "fixed")["name"] == "fixed"
    sqlite = Database("sqlite:///" + str(tmp_path / "restored.db"), import_legacy=False)
    try:
        import_data(target, db=sqlite)
        with db.engine.connect() as a, sqlite.engine.connect() as b:
            for table in metadata.sorted_tables:
                assert {tuple(r) for r in a.execute(select(table))} == {tuple(r) for r in b.execute(select(table))}
        sqlite_export = tmp_path / "sqlite-export.json"
        export_data(sqlite_export, db=sqlite)
        with db.transaction() as conn:
            for table in reversed(metadata.sorted_tables):
                conn.execute(table.delete())
        import_data(sqlite_export, db=db)
        assert identity.lookup(token["token"]).user_id == user["id"]
        assert Vault().get("proxy") == "http://user:password@fixture.invalid:80"
    finally:
        sqlite.engine.dispose()


def test_export_refuses_live_session_and_job(service_db, tmp_path):
    db = service_db
    with db.state("service-sessions") as state:
        state["records"]["active"] = {"state": "cleanup_pending"}
    with pytest.raises(ValueError, match="回收"):
        export_data(tmp_path / "busy.json", db=db)
    with db.state("service-sessions") as state:
        state["records"].clear()
    db.put("jobs", "running", {"status": "running"})
    with pytest.raises(ValueError, match="后台任务"):
        export_data(tmp_path / "busy.json", db=db)
    db.delete("jobs", "running")
    with db.state("manager-fixture") as state:
        state["records"]["browser"] = {"slot": True}
    with pytest.raises(ValueError, match="额度或租约"):
        export_data(tmp_path / "busy.json", db=db)


def test_three_legacy_databases_import_once(service_db):
    db = service_db
    old = Database("sqlite:///" + str(db.root / "sleight.db"), import_legacy=False)
    with old.transaction() as conn:
        conn.execute(metadata.tables["hosts"].insert().values(name="legacy", created_at="now", updated_at="now"))
        conn.execute(metadata.tables["events"].insert().values(ts="now", host="legacy", kind="import", ok=1))
    old.engine.dispose()
    for filename, table, key, value in (
        ("runtime.db", "ledger", "old-runtime", {"records": {"fixed": {"slot": True}}, "owners": {}}),
        ("jobs.db", "jobs", "old-job", {"status": "ok", "result": [1]}),
    ):
        with sqlite3.connect(db.root / filename) as conn:
            conn.execute(f"CREATE TABLE {table}(id TEXT PRIMARY KEY, body TEXT)")
            conn.execute(f"INSERT INTO {table} VALUES (?, ?)", (key, json.dumps(value)))
        db.delete("migration", filename)
    db.delete("migration", "deployed")
    db._import_legacy()
    db._import_legacy()
    with db.engine.connect() as conn:
        assert len(conn.execute(select(metadata.tables["events"])).all()) == 1
    with db.state("old-runtime") as state:
        assert state["records"]["fixed"]["slot"]
    assert db.get("jobs", "old-job")["result"] == [1]
    for filename in ("sleight.db", "runtime.db", "jobs.db"):
        assert (db.root / filename).is_file()
    (db.root / "agent-secrets.json").write_text('{"old":"private"}', encoding="utf-8")
    vault = Vault()
    vault.import_file("agent-secrets.json", "agent:")
    assert vault.get("agent:old") == "private"
    vault.put("agent:old", "new")
    vault.import_file("agent-secrets.json", "agent:")
    assert vault.get("agent:old") == "new"


def test_durable_credentials_keep_grant_chain(service_db):
    app = create_app(token="root", runtime=False)
    identity = app.state.auth.identity
    delegate = identity.issue("admin", scopes=["delegate"])
    principal = identity.lookup(delegate["token"])
    short = identity.delegate(principal, "pyp:alice", "Alice", 30)
    with TestClient(app) as client:
        child = client.post("/api/v1/tokens", headers={"X-Sleight-Token": short["token"]},
                            json={"name": "fin", "scopes": ["execute"]}).json()
    assert child["parent"] == delegate["id"]
    identity.revoke(short["id"])
    assert identity.lookup(child["token"])
    identity.revoke(delegate["id"])
    assert identity.lookup(child["token"]) is None


def test_user_disabled_invalidates_descendants(service_db):
    identity = Identity()
    identity.ensure_admin("root")
    parent = identity.lookup("root")
    delegated = identity.delegate(parent, "pyp:alice", "Alice", 900)
    admin = identity.user("admin")
    identity.save_user({**admin, "active": False})
    assert identity.lookup(delegated["token"]) is None
    assert not identity.valid_id(delegated["id"])


def test_confirmed_cleanup_only_releases_own_environment(service_db):
    lease = DatabaseLease(service_db)
    first = lease.acquire("service:one:same", ttl=30)
    second = lease.acquire("service:two:same", ttl=30)
    lease.cleaned("same", namespace="service:one")
    assert not lease.renew("service:one:same", first, ttl=30)
    assert lease.renew("service:two:same", second, ttl=30)


@contextmanager
def fake_session(profile, options):
    yield type("Session", (), {"title": lambda self: "fixture"})()


def test_client_disconnect_and_restart_recover_with_cleanup_retry(service_db, monkeypatch):
    db = service_db
    identity = Identity()
    identity.ensure_admin("root")
    user = identity.lookup("root")
    supervisor = Supervisor(factory=fake_session)
    profile = {"id": "p", "owner": "admin", "name": "fixture", "environment": "native", "source_id": "p", "ephemeral": False}
    db.put("profiles", "p", profile)
    first = supervisor.open({"profile": "p"}, user, "once")
    assert supervisor.open({"profile": "p"}, user, "once")["id"] == first["id"]
    supervisor.update(first["id"], expires=0)
    supervisor.sweep()
    assert supervisor.actors[first["id"]].done.wait(3)
    supervisor.sweep()
    assert not supervisor.actors
    restarted = Supervisor()
    with db.state("service-sessions") as state:
        state["records"]["abandoned"] = {**first, "id": "abandoned", "state": "running", "worker": "dead", "expires": time.time() + 3600}
        state["owners"]["dead"] = 0
    def failure(*args, **kwargs):
        raise errors.InstanceError("unreachable")
    monkeypatch.setattr(restarted.controller, "cleanup", failure)
    restarted.sweep()
    assert restarted.get("abandoned", user)["state"] == "cleanup_pending"
    with pytest.raises(errors.Busy):
        restarted.open({"profile": "p"}, user, "different")
    monkeypatch.setattr(restarted.controller, "cleanup", lambda *a, **kw: None)
    restarted.sweep()
    assert restarted.get("abandoned", user)["state"] == "released"


def test_fixed_alias_delete_does_not_destroy_another_users_browser(service_db, monkeypatch):
    from sleight.service import execution
    identity = Identity()
    identity.ensure_admin("root")
    root = identity.lookup("root")
    supervisor = Supervisor(factory=fake_session)
    profile = {"id": "one", "owner": "admin", "name": "shared", "environment": "local/manager", "source_id": "shared",
               "external": True, "ephemeral": False, "touched": time.time()}
    service_db.put("profiles", "one", profile)
    service_db.put("profiles", "two", {**profile, "id": "two"})
    opened = supervisor.open({"profile": "one"}, root, "open")
    with pytest.raises(errors.Busy):
        supervisor.delete_profile("two", root)
    supervisor.close(opened["id"], root)
    stopped, deleted = [], []
    @contextmanager
    def manager_for(environment):
        class Manager:
            def stop(self, pid):
                stopped.append(pid)
            def delete_profile(self, pid, *, force):
                deleted.append(pid)
        yield Manager()
    monkeypatch.setattr(execution, "manager_for", manager_for)
    supervisor.delete_profile("two", root)
    assert stopped == ["shared"] and not deleted
    assert service_db.get("profiles", "two") is None
    assert service_db.get("profiles", "one")
