"""Probe an installed wheel inside a disposable local Manager container."""

import json
import os
import threading
import time
from contextlib import suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import psutil

import sleight
from sleight import FetchResponse, ResponseTooLarge, Transport, connect
from sleight.core.errors import SleightError, TimeoutError
from sleight.core.types import InstanceStatus
from sleight.deploy.spec import CONTAINER_PORT
from sleight.providers import CloakBrowserManager, ProfileSpec

calls = []


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        calls.append(self.path)
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        if self.path == "/":
            self.send_header("Content-Type", "text/html")
            self.send_header("Set-Cookie", "sid=linux-fixture; HttpOnly; Path=/")
            data = b"<html><title>Linux fixture</title></html>"
        elif self.path == "/large":
            data = b"x" * 512
        elif self.path == "/slow":
            data = b"xx"
        else:
            data = json.dumps({"cookie": self.headers.get("Cookie"), "message": "中文"}).encode()
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        try:
            if self.path == "/slow":
                self.wfile.write(data[:1])
                self.wfile.flush()
                time.sleep(.5)
                self.wfile.write(data[1:])
            else:
                self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass


server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
server.daemon_threads = True
thread = threading.Thread(target=server.serve_forever, daemon=True)
thread.start()
manager = CloakBrowserManager(f"http://127.0.0.1:{CONTAINER_PORT}", token=os.environ["AUTH_TOKEN"])
profile = None


def drivers():
    return sum(any("playwright/driver/node" in arg for arg in process.info["cmdline"] or [])
               for process in psutil.process_iter(["cmdline"]))


def wait_for_driver_cleanup(baseline):
    for _ in range(100):
        if drivers() == baseline and manager.status(profile.id) == InstanceStatus.STOPPED:
            return
        time.sleep(.1)
    raise AssertionError("browser closure left an active profile or Playwright driver")


baseline = drivers()
try:
    profile = manager.create_profile(ProfileSpec(name="browser-fetch-linux-qa", fingerprint_seed=42,
        geoip=False, set_google_default=False, restore_session=False), launch=True)
    endpoint = manager.endpoint(profile.id)
    with connect(endpoint.ws_url, headers=dict(endpoint.headers), track_runtime=False) as session:
        base = f"http://127.0.0.1:{server.server_port}"
        session.open(base)
        before = session.url(), session.target_id, session.history()
        page_ids = {target["targetId"] for target in session.call("Target.getTargets")["targetInfos"]
                    if target["type"] == "page"}
        for _ in range(10):
            response = session.fetch("/echo")
            assert isinstance(response, FetchResponse) and response.ok
            assert response.json() == {"cookie": "sid=linux-fixture", "message": "中文"}
        try:
            session.fetch("/large", max_bytes=64)
            raise AssertionError("response limit was ignored")
        except ResponseTooLarge:
            pass
        try:
            session.fetch("/slow", timeout=.15)
            raise AssertionError("request timeout was ignored")
        except TimeoutError:
            pass
        assert calls.count("/slow") == 1
        assert session.fetch("/echo").ok
        assert (session.url(), session.target_id, session.history()) == before
        assert {target["targetId"] for target in session.call("Target.getTargets")["targetInfos"]
                if target["type"] == "page"} == page_ids
    manager.stop(profile.id)
    wait_for_driver_cleanup(baseline)
    manager.ensure_ready(profile.id)
    endpoint = manager.endpoint(profile.id)
    transport = Transport.connect(endpoint.ws_url, headers=dict(endpoint.headers))
    try:
        with suppress(SleightError):
            transport.call("Browser.close")
    finally:
        transport.close()
    wait_for_driver_cleanup(baseline)
    print(json.dumps({"sleight": sleight.__version__, "platform": "Linux Cloak Manager",
                      "checks": "cookies, JSON, page reuse, response limit, timeout, recovery, stop and Browser.close cleanup"}))
finally:
    if profile:
        manager.delete_profile(profile.id, force=True)
    server.shutdown()
    server.server_close()
    thread.join(2)
