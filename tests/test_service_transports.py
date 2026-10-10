"""Actual HTTP, stdio MCP and cross-project proxy protocols on localhost."""

import asyncio
import os
import socket
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import uvicorn
import websockets
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from sleight.deploy.api.app import create_app
from sleight.service.identity import Identity


@contextmanager
def factory(profile, options):
    yield SimpleNamespace(title=lambda: "fixture", url=lambda: "about:blank", target_id="page",
                          cdp_session_id="cdp", transport=SimpleNamespace(close=lambda: None))


@pytest.fixture
def service(service_db):
    db = service_db
    app = create_app(token="root", runtime=False)
    app.state.supervisor.factory = factory
    def create(body, principal, request_id):
        pid = "fixture-" + principal.user_id
        row = {"id": pid, "owner": principal.user_id, "environment": "native", "source_id": pid,
               "ephemeral": False, "name": "fixture", "state": "idle", "touched": time.time()}
        db.put("profiles", pid, row)
        return row
    app.state.supervisor.controller.create = create
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning"))
    thread = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
    thread.start()
    try:
        for _ in range(100):
            if server.started:
                break
            time.sleep(.05)
        assert server.started
        yield f"http://127.0.0.1:{port}", app
    finally:
        server.should_exit = True
        thread.join(timeout=20)


def test_official_stdio_mcp_handshake_and_release(service):
    url, _ = service
    async def run():
        params = StdioServerParameters(command=sys.executable, args=["-m", "sleight.agent.mcp"],
                                       env={**os.environ, "SLEIGHT_UPSTREAM": url, "SLEIGHT_TOKEN": "root"})
        async with stdio_client(params) as streams, ClientSession(*streams) as client:
            assert (await client.initialize()).serverInfo.name == "sleight-client"
            assert "browser_release" in [r.name for r in (await client.list_tools()).tools]
            assert not (await client.call_tool("browser_session", {"action": "info"})).isError
            assert not (await client.call_tool("browser_release", {})).isError
    asyncio.run(run())


def test_http_mcp_fetch_has_enough_actor_budget(service, monkeypatch):
    url, app = service
    calls = []

    def invoke(sid, user, body):
        calls.append(body)
        return {"result": {"content": [{"type": "text", "text": "{}"}], "isError": False}}

    monkeypatch.setattr(app.state.supervisor, "call", invoke)
    response = httpx.post(url + "/mcp", headers={"X-Sleight-Token": "root",
        "Accept": "application/json, text/event-stream"}, json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
            "name": "browser_fetch", "arguments": {"url": "/api/echo", "timeout": 120}}})
    assert response.status_code == 200, response.text
    assert not response.json()["result"]["isError"]
    assert calls[0]["timeout"] > 120


def test_agent_delegate_only_connection_and_identity(service):
    url, app = service
    identity = Identity()
    delegate = identity.issue("admin", scopes=["delegate"])["token"]
    with httpx.Client(base_url=url, headers={"X-Sleight-Token": "root"}, timeout=20) as client:
        response = client.post("/api/agents", json={"url": url, "token": delegate, "name": "local test"})
        assert response.status_code == 200, response.text
        pid = response.json()["id"]
        prefix = f"/agent/{pid}"
        me = client.get(prefix + "/api/auth/me")
        assert me.status_code == 200, me.text
        assert me.json()["user"]["id"] != "admin"
        assert client.get(prefix + "/api/runtime/platform").status_code == 200
        assert client.get(prefix + "/api/agents").status_code == 403
        page = client.get(prefix + "/")
        assert "assets/" in page.text and delegate not in page.text
        profile = client.post(prefix + "/api/v1/profiles", json={}, headers={"Idempotency-Key": "agent-create"}).json()
        opened = client.post(prefix + "/api/v1/sessions", json={"profile": profile["id"]}).json()
        assert opened["owner"] == me.json()["user"]["id"]
        async def events():
            async with websockets.connect(url.replace("http", "ws", 1) + prefix + f"/api/v1/sessions/{opened['id']}/events",
                                          additional_headers={"X-Sleight-Token": "root"}) as stream:
                assert '"running"' in await asyncio.wait_for(stream.recv(), 10)
        asyncio.run(events())
        client.delete(prefix + f"/api/v1/sessions/{opened['id']}").raise_for_status()
        client.delete(f"/api/agents/{pid}").raise_for_status()
    assert app.state.supervisor.get(opened["id"], identity.lookup("root"))["state"] == "released"


def test_real_pyp_proxy_identity_sse_ws_and_assets(service):
    root = os.environ.get("SLEIGHT_PYP_TEST_ROOT")
    if not root:
        pytest.skip("Set SLEIGHT_PYP_TEST_ROOT to validate the isolated pyp proxy")
    url, app = service
    job = app.state.jobs.start("fixture", "local", lambda say: say("probe ready"))
    for _ in range(100):
        if job.finished:
            break
        time.sleep(.01)
    delegate = Identity().issue("admin", scopes=["delegate"])["token"]
    result = subprocess.run([str(Path(root) / ".venv/Scripts/python.exe"),
                             str(Path(__file__).parent / "fixtures/pyp_proxy_probe.py"), url, delegate, job.id],
                            cwd=root, capture_output=True, text=True, timeout=90)
    assert result.returncode == 0, result.stdout + result.stderr
