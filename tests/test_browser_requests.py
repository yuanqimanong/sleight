"""Browser context reuse, bounded reads and SDK/MCP parity; no external sites."""

import json
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from sleight import FetchResponse, ResponseTooLarge, connect
from sleight.agent.gateway import Gateway
from sleight.agent.mcp import MCPServer
from sleight.client import RemoteSession, ServiceClient
from sleight.core.errors import ProtocolError, TimeoutError
from sleight.core.session import Session
from sleight.deploy.api.app import create_app

from .test_service import ClientTransport


@pytest.fixture
def origin():
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def respond(self, method):
            body = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode()
            calls.append(
                {
                    "method": method,
                    "path": self.path,
                    "cookie": self.headers.get("Cookie"),
                    "body": body,
                    "custom": self.headers.get("X-Test"),
                    "referer": self.headers.get("Referer"),
                }
            )
            if self.path == "/redirect":
                self.send_response(302)
                self.send_header("Location", "/echo")
                self.end_headers()
                return
            if self.path == "/slow":
                time.sleep(0.5)
            status = 403 if self.path == "/denied" else 200
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            if self.path == "/":
                self.send_header("Set-Cookie", "sid=fixture; HttpOnly; Path=/")
                data = b"<html><title>Fixture</title><h1>fixture</h1></html>"
                self.send_header("Content-Type", "text/html")
            elif self.path == "/large":
                data = b"x" * (1024 * 1024 + 1)
            elif self.path == "/stream":
                data = b"x" * 1024
            elif self.path == "/utf8":
                data = "中文 café 🌏".encode()
            elif self.path == "/drip":
                data = b"xx"
            else:
                data = json.dumps(calls[-1]).encode()
            if self.path != "/stream":
                self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            if method == "HEAD":
                return
            try:
                if self.path == "/drip":
                    self.wfile.write(data[:1])
                    self.wfile.flush()
                    time.sleep(0.5)
                    self.wfile.write(data[1:])
                elif self.path == "/utf8":
                    for offset in range(0, len(data), 2):
                        self.wfile.write(data[offset : offset + 2])
                        self.wfile.flush()
                elif self.path == "/stream":
                    for offset in range(0, len(data), 32):
                        self.wfile.write(data[offset : offset + 32])
                        self.wfile.flush()
                        time.sleep(0.002)
                else:
                    self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

        def do_GET(self):
            self.respond("GET")

        def do_POST(self):
            self.respond("POST")

        def do_HEAD(self):
            self.respond("HEAD")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


@pytest.mark.parametrize(
    "url,options",
    [
        ("file:///private", {}),
        ("http://u:p@example.test/", {}),
        ("/echo", {"method": "TRACE"}),
        ("http://@example.test/", {}),
        ("/echo", {"headers": {"Cookie": "secret"}}),
        ("/echo", {"headers": {"Sec-Fetch-Site": "same-origin"}}),
        ("/echo", {"headers": {"X-Test": "a\nb"}}),
        ("/echo", {"body": "data"}),
        ("/echo", {"max_bytes": 1048577}),
        ("/echo", {"timeout": float("nan")}),
        ("/echo", {"timeout": float("inf")}),
        ("/echo", {"timeout": True}),
        ("/echo", {"method": "POST", "body": "x" * 1048577}),
    ],
)
def test_invalid_requests_never_reach_the_browser(url, options):
    session = object.__new__(Session)
    session.call = Mock()
    with pytest.raises(ValueError):
        session.fetch(url, **options)
    session.call.assert_not_called()


def test_request_urls_and_body_are_encoded_as_data():
    session = object.__new__(Session)
    session.call = Mock(
        return_value={
            "result": {
                "value": {
                    "url": "http://fixture/",
                    "status": 200,
                    "status_text": "OK",
                    "headers": {},
                    "text": "{}",
                    "redirected": False,
                }
            }
        }
    )
    url = "/echo?q=\"');window.compromised=true;//"
    body = "hello\n'世界"
    assert session.fetch(url, method="POST", body=body).json() == {}
    expression = session.call.call_args.args[1]["expression"]
    arguments = json.loads(expression[expression.rfind("})(") + 3 : -1])
    assert arguments["url"] == url
    assert arguments["body"] == body


def test_mcp_bounds_response_size_and_keeps_http_errors_as_data():
    session = Mock()
    session.fetch.return_value = FetchResponse("http://fixture", 403, "Forbidden", {}, "denied")
    gateway = Gateway(session)
    server = MCPServer(lambda: gateway)
    response = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "browser_fetch",
                "arguments": {"url": "http://fixture", "max_bytes": 9999999},
            },
        }
    )["result"]
    assert not response["isError"]
    assert json.loads(response["content"][0]["text"])["http_ok"] is False
    assert session.fetch.call_args.kwargs["max_bytes"] == 262144
    assert not gateway.fetch("http://fixture", method="POST").ok
    assert session.fetch.call_count == 1


@pytest.mark.parametrize("remaining", [0, 0.05, 2])
def test_remote_fetch_respects_remaining_task_budget(monkeypatch, remaining):
    monkeypatch.setattr("sleight.client.time.monotonic", lambda: 100)
    session = object.__new__(RemoteSession)
    session.client = SimpleNamespace(deadline=lambda: 100 + remaining)
    session._rpc = Mock()
    if remaining < 0.1:
        with pytest.raises(TimeoutError, match="budget"):
            session.fetch("/echo")
        session._rpc.assert_not_called()
    else:
        session.fetch("/echo")
        assert session._rpc.call_args.kwargs["kwargs"]["timeout"] == remaining


@pytest.mark.integration
def test_browser_fetch_reuses_httponly_cookie_and_keeps_page(live_endpoint, origin):
    base, calls = origin
    with connect(live_endpoint, track_runtime=False) as session:
        session.open(base)
        assert "sid=" not in session.eval("document.cookie")
        before = session.url(), session.target_id, session.history()
        pages_before = {
            target["targetId"]
            for target in session.call("Target.getTargets")["targetInfos"]
            if target["type"] == "page"
        }
        response = session.fetch("/echo", headers={"X-Test": "fixture"})
        assert response.ok and response.json()["cookie"] == "sid=fixture"
        assert response.json()["custom"] == "fixture"
        assert (session.url(), session.target_id, session.history()) == before
        posted = session.fetch("/echo", method="POST", body="hello")
        assert posted.json()["body"] == "hello"
        assert sum(c["method"] == "POST" for c in calls) == 1
        assert session.fetch("/utf8").text == "中文 café 🌏"
        assert session.fetch("/redirect").redirected
        assert session.fetch("/echo", method="HEAD").text == ""
        assert not session.fetch("/denied").ok
        assert {
            target["targetId"]
            for target in session.call("Target.getTargets")["targetInfos"]
            if target["type"] == "page"
        } == pages_before


@pytest.mark.integration
def test_browser_fetch_cancels_large_or_slow_reads_without_replay(live_endpoint, origin):
    base, calls = origin
    with connect(live_endpoint) as session:
        session.open(base)
        with pytest.raises(ResponseTooLarge):
            session.fetch("/large", max_bytes=64)
        with pytest.raises(ResponseTooLarge):
            session.fetch("/stream", max_bytes=64)
        assert sum(c["path"] == "/stream" for c in calls) == 1
        for path in ("/slow", "/drip"):
            started = time.monotonic()
            with pytest.raises(TimeoutError):
                session.fetch(path, timeout=0.15)
            assert time.monotonic() - started < 2
        assert session.fetch("/echo").ok
        assert sum(c["path"] == "/slow" for c in calls) == 1
        assert sum(c["path"] == "/drip" for c in calls) == 1


@pytest.mark.integration
def test_browser_cors_and_navigation_referrer_policy(live_endpoint, origin):
    base, _ = origin
    with connect(live_endpoint) as session:
        session.open(base)
        other = base.replace("127.0.0.1", "localhost")
        with pytest.raises(ProtocolError, match="CORS"):
            session.fetch(other + "/echo")
        session.open(base + "/echo", referrer=other + "/source?private=query")
        result = json.loads(session.text())
        assert result["referer"] == other + "/"
        with pytest.raises(ValueError):
            session.open(base, referrer="file:///private")
        server = MCPServer(lambda: Gateway(session))
        reply = server.handle(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "browser_session",
                    "arguments": {
                        "action": "open",
                        "url": base + "/echo",
                        "referrer": other + "/source?private=query",
                    },
                },
            }
        )["result"]
        assert not reply["isError"], reply
        assert json.loads(session.text())["referer"] == other + "/"


@pytest.mark.integration
def test_browser_fetch_sdk_and_http_mcp_share_user_session(service_db, live_endpoint, origin):
    base, _ = origin
    app = create_app(token="fixture-root", runtime=False)

    @contextmanager
    def factory(profile, options):
        with connect(live_endpoint, track_runtime=False) as session:
            yield session

    app.state.supervisor.factory = factory
    service_db.put(
        "profiles",
        "fetch-fixture",
        {
            "id": "fetch-fixture",
            "owner": "admin",
            "environment": "native",
            "source_id": "fixture",
            "name": "Fixture",
            "ephemeral": False,
            "state": "idle",
            "touched": time.time(),
        },
    )
    with (
        TestClient(app) as web,
        ServiceClient(
            "http://testserver", "fixture-root", transport=ClientTransport(web)
        ) as client,
        client.session("fetch-fixture") as session,
    ):
        session.open(base, referrer=base + "/source")
        response = session.fetch("/echo")
        assert isinstance(response, FetchResponse)
        assert response.json()["cookie"] == "sid=fixture"
        reply = web.post(
            "/mcp",
            headers={
                "X-Sleight-Token": "fixture-root",
                "X-Sleight-Session": session.id,
                "Accept": "application/json, text/event-stream",
            },
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "browser_fetch", "arguments": {"url": "/echo"}},
            },
        )
        assert reply.status_code == 200, reply.text
        value = reply.json()["result"]
        assert not value["isError"], value
        assert (
            json.loads(json.loads(value["content"][0]["text"])["text"])["cookie"] == "sid=fixture"
        )
        with pytest.raises(ResponseTooLarge):
            session.fetch("/large", max_bytes=64)
        assert session.fetch("/echo").ok
