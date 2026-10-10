"""Local Docker Linux Manager and Windows native through the service SDK."""

import inspect
import json
import os
import socket
import textwrap
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler
from pathlib import Path

import httpx
import pytest
import uvicorn

from sleight.client import ServiceClient
from sleight.core import errors
from sleight.core.types import DomReady
from sleight.deploy import Deployer, DeploySpec, Host, LocalRunner, Store
from sleight.deploy.api.app import create_app
from sleight.deploy.ops import ExtensionOps
from sleight.runtime.config import Templates
from sleight.service.identity import Identity

pytestmark = pytest.mark.skipif(os.environ.get("SLEIGHT_SERVICE_LIVE") != "1", reason="Dedicated local Docker/native service test")


@pytest.fixture
def live(tmp_path, monkeypatch, service_db):
    name = "sleight-service-" + uuid.uuid4().hex[:8]
    spec = DeploySpec(name=name, container_name=name, dir=str(tmp_path / "deployment").replace("\\", "/"),
                      data_volume=name + "-store", port=19069, mem_limit="2500m", shm_size="512m", max_running=1, resource_key=name)
    runner = LocalRunner()
    deployer = Deployer(spec, runner)
    store = Store()
    store.put_host(Host(name="local"))
    store.put_deployment("local", "manager", spec)
    entry = store.require_deployment("local", "manager")
    deployer = Deployer(entry.spec, runner)
    server = None
    try:
        deployer.apply(pull=False)
        extension = ExtensionOps(deployer).push(Path(__file__).parent / "fixtures" / "extension")
        template = Templates().save({"name": "MV3 probe", "extension_paths": [extension.container_path]})
        identity = Identity()
        identity.ensure_admin("live-admin")
        user = identity.save_user({"name": "fin fixture", "default_environment": entry.ref, "default_template": template["id"], "limit": 2})
        token = identity.issue(user["id"])["token"]
        app = create_app(token="live-admin")
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, log_level="warning"))
        thread = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
        thread.start()
        for _ in range(100):
            if server.started:
                break
            time.sleep(.1)
        assert server.started
        yield {"url": f"http://127.0.0.1:{port}", "token": token, "app": app, "entry": entry}
    finally:
        if server:
            server.should_exit = True
            thread.join(timeout=30)
        deployer.destroy(purge_data=True)
        runner.run(["docker", "volume", "rm", entry.spec.data_volume])


def test_service_linux_manager_plugin_capture_and_capacity(live):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/api/echo":
                body = json.dumps({"cookie": self.headers.get("Cookie")}).encode()
                mime = "application/json"
            elif self.path.endswith(".js"):
                body = b"window.fixtureReady=true;"
                mime = "application/javascript"
            elif self.path.endswith(".png"):
                body = b"fixture"
                mime = "image/png"
            else:
                body = b'<html><title>Service fixture</title><h1>Reuters fixture</h1><input id="name"><button id="button" onclick="document.querySelector(\"h1\").textContent=\"clicked\"">Run</button><script src="/probe.js"></script><img src="/blocked.png"></html>'
                mime = "text/html"
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Set-Cookie", "sid=service-fixture; HttpOnly; Path=/")
            self.end_headers()
            self.wfile.write(body)
        def log_message(self, *args):
            pass
    fixture_script = "import json\nfrom http.server import BaseHTTPRequestHandler, ThreadingHTTPServer\n" + textwrap.dedent(inspect.getsource(Handler)) + "\nThreadingHTTPServer(('127.0.0.1',18779),Handler).serve_forever()"
    LocalRunner().run(["docker", "exec", "-d", live["entry"].spec.container_name, "python3", "-c", fixture_script], check=True)
    try:
        with ServiceClient(live["url"], live["token"]) as client:
            profile = client.create_profile(name="Linux fixture", fingerprint_seed=34567)
            same = client.create_profile(request_id="same-profile", name="idle")
            assert client.create_profile(request_id="same-profile", name="idle")["id"] == same["id"]
            with client.session(profile["id"], human=True) as session:
                with session.block(types=["Image"]) as stats, session.capture_resources(predicate=lambda r: r.url.endswith(".js")) as cap:
                    try:
                        session.open("http://127.0.0.1:18779/", wait=DomReady())
                    except errors.SleightError as exc:
                        raise AssertionError(repr(live["app"].state.supervisor.actors[session.id].last_error)) from exc
                    session.pump_events(2)
                    assert session.title() == "Service fixture"
                    before = session.url(), session.target_id
                    assert session.fetch("/api/echo").json()["cookie"] == "sid=service-fixture"
                    response = httpx.post(live["url"] + "/mcp", headers={
                        "X-Sleight-Token": live["token"], "X-Sleight-Session": session.id,
                        "Accept": "application/json, text/event-stream"}, json={
                        "jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
                            "name": "browser_fetch", "arguments": {"url": "/api/echo"}}})
                    assert response.status_code == 200, response.text
                    result = response.json()["result"]
                    assert not result["isError"], result
                    assert json.loads(json.loads(result["content"][0]["text"])["text"])["cookie"] == "sid=service-fixture"
                    assert (session.url(), session.target_id) == before
                    assert session.eval("document.documentElement.dataset.sleightExtension") == "loaded"
                    assert cap.urls() and all(url.endswith(".js") for url in cap.urls())
                    assert stats.blocked >= 1 and stats.by_type["Image"] >= 1
                    assert session.query("h1").text() == "Reuters fixture"
                    assert session.parse(xpath=True).xpath("//h1")[0].text == "Reuters fixture"
                    session.type("#name", "Sleight")
                    assert session.eval("document.querySelector('#name').value") == "Sleight"
                    assert session.screenshot().startswith(b"\x89PNG")
                    # A fresh socket/cookie context must not disturb the collection page.
                    assert session.probe_exit_ip('({ip:"192.0.2.25",city:"fixture"})')["ip"] == "192.0.2.25"
                    assert session.title() == "Service fixture"
                with pytest.raises(errors.Busy):
                    client.session(same["id"])
                assert not any(type(value).__name__ == "ResourceTracker" for value in live["app"].state.supervisor.actors[session.id].objects.values())
                assert session.clear_site_data("http://127.0.0.1:18779").origin
                old_page = session.parse(xpath=True)
                session.open("http://127.0.0.1:18779/", wait=DomReady())
                with pytest.raises(errors.StaleRef):
                    old_page.xpath("//h1")
            assert client.request("GET", "api/v1/profiles") == [] or all(p["id"] != profile["id"] for p in client.request("GET", "api/v1/profiles"))
    finally:
        pass  # The dedicated Manager container owns the fixture process.


def test_service_native_session(live):
    from sleight.runtime.native import discover_browsers
    browsers = discover_browsers()
    if not browsers:
        pytest.skip("No native Chromium installed")
    with ServiceClient(live["url"], "live-admin") as client:
        profile = client.create_profile(name="Native fixture", environment="native", binary=browsers[0]["binary"])
        with client.session(profile["id"]) as session:
            session.open("data:text/html,<title>Native service</title><h1>ready</h1>", wait=DomReady())
            assert session.title() == "Native service"
            assert session.query("h1").text() == "ready"
            assert session.screenshot().startswith(b"\x89PNG")


def test_console_cloak_templates_and_temporary_cleanup(live):
    with ServiceClient(live["url"], "live-admin") as client:
        for index in range(3):
            response = httpx.post(live["url"] + "/api/v1/profiles", headers={"X-Sleight-Token": "live-admin", "Idempotency-Key": uuid.uuid4().hex},
                json={"name": "Console template " + str(index), "environment": live["entry"].ref,
                      "launch_template_id": "builtin-cloak-" + str(index), "ephemeral": False}, timeout=60)
            assert response.status_code == 200, response.text
            profile = response.json()
            try:
                with client.session(profile["id"], human=False) as session:
                    session.open("about:blank")
                    data = client.request("GET", "api/v1/fleet")
                    assert any(r["profile_id"] == profile["id"] and r["kind"] == "cloak" and r["status"] == "running" for r in data["instances"])
                    view = client.request("GET", "api/v1/fleet/inspect?ref=profile:" + profile["id"])
                    assert view["image"].startswith("data:image/jpeg;base64,")
            finally:
                client.request("DELETE", "api/v1/profiles/" + profile["id"])
        temporary = client.create_profile(name="Temporary console", environment=live["entry"].ref,
            launch_template_id="builtin-cloak-0", ephemeral=True)
        assert temporary["fingerprint_seed"] == "random"
        with client.session(temporary["id"], human=False):
            assert client.request("GET", "api/v1/fleet")["counts"]["temporary"] == 1
        assert client.request("GET", "api/v1/fleet")["counts"]["temporary"] == 0


def test_fin_pool_calls_service(live):
    import subprocess
    fin = os.environ.get("SLEIGHT_FIN_TEST_ROOT")
    if not fin:
        pytest.skip("Set SLEIGHT_FIN_TEST_ROOT for the isolated fin round")
    env = {**os.environ, "SLEIGHT_TEST_UPSTREAM": live["url"], "SLEIGHT_TEST_TOKEN": live["token"]}
    result = subprocess.run([str(Path(fin) / ".venv/Scripts/python.exe"), "tools/test_offline.py",
                             "tests/test_sleight_service.py::test_real_fin_service_round", "-q"],
                            cwd=fin, env=env, capture_output=True, text=True, timeout=240)
    assert result.returncode == 0, result.stdout + result.stderr


def test_console_edit_manager_keeps_identity_cookie_and_plugin(live):
    """Edit the real Linux profile through the same endpoint used by the UI."""
    from sleight.providers.compat import extension_list
    from sleight.service.execution import manager_for

    fixture = textwrap.dedent('''
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Set-Cookie", "editor_probe=preserved; Max-Age=86400; Path=/")
                self.end_headers()
                self.wfile.write(b"<title>Cookie fixture</title>")
            def log_message(self, *args):
                pass
        ThreadingHTTPServer(("127.0.0.1", 18780), Handler).serve_forever()
    ''')
    LocalRunner().run(["docker", "exec", "-d", live["entry"].spec.container_name, "python3", "-c", fixture], check=True)
    with ServiceClient(live["url"], "live-admin") as client:
        profile = client.create_profile(name="Manager edit fixture", environment=live["entry"].ref,
            launch_template_id="builtin-cloak-0", ephemeral=False)
        ref = "profile:" + profile["id"]
        try:
            with client.session(profile["id"], human=False) as session:
                session.open("http://127.0.0.1:18780/", wait=DomReady())
                assert any(c["name"] == "editor_probe" for c in session.cookies(["http://127.0.0.1:18780/"]))
                with pytest.raises(errors.Busy):
                    client.request("POST", "api/v1/fleet/edit", json={"ref": ref, "changes": {"notes": "running"}})
            configuration = client.request("GET", "api/v1/fleet/configuration?ref=" + ref)
            assert configuration["generation"] == "modern"
            with manager_for(live["entry"].ref) as manager:
                original = manager.get_profile(profile["source_id"])
            edited = client.request("POST", "api/v1/fleet/edit", json={"ref": ref, "changes": {
                "name": "Manager renamed", "notes": "edit preserved data", "fingerprint_seed": 45678,
                "launch_template_id": "builtin-cloak-1"}})
            assert edited["id"] == profile["id"] and edited["source_id"] == profile["source_id"]
            with manager_for(live["entry"].ref) as manager:
                updated = manager.get_profile(profile["source_id"])
                assert updated["id"] == original["id"]
                assert updated["fingerprint_seed"] == 45678
                assert extension_list(updated) == extension_list(original)
            with client.session(profile["id"], human=False) as session:
                assert any(c["name"] == "editor_probe" and c["value"] == "preserved"
                    for c in session.cookies(["http://127.0.0.1:18780/"]))
            configuration = client.request("GET", "api/v1/fleet/configuration?ref=" + ref)
            assert configuration["name"] == "Manager renamed" and configuration["notes"] == "edit preserved data"
        finally:
            client.request("DELETE", "api/v1/profiles/" + profile["id"])
