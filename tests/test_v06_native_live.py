"""Opt-in OS-native browser, extension, proxy and preview contracts."""
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from sleight.core.session import Session
from sleight.core.transport import Transport
from sleight.providers.plain import Plain
from sleight.runtime.native import NativeSupervisor

pytestmark = pytest.mark.integration


def test_native_extension_proxy_preview_and_cleanup(tmp_path, monkeypatch):
    binary = os.environ.get("SLEIGHT_NATIVE_BINARY")
    if not binary:
        pytest.skip("Set SLEIGHT_NATIVE_BINARY to an extension-capable Chromium binary")
    monkeypatch.setenv("SLEIGHT_HOME", str(tmp_path))
    requests = []

    class Proxy(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<title>local proxy fixture</title><h1>proxy reached</h1>")

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Proxy)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    native = NativeSupervisor(limit=1)
    profile = native.create({"name": "local integration", "binary": binary, "headless": True,
                             "no_sandbox": os.name != "nt", "fingerprint_seed": 1234,
                             "extension_paths": [str(Path(__file__).parent / "fixtures/extension")],
                             "proxy": f"http://127.0.0.1:{server.server_port}"})
    try:
        row = native.start(profile["id"])
        assert row["memory_bytes"] > 0 and row["fingerprint"] == 1234
        endpoint = Plain(f"http://127.0.0.1:{row['port']}").endpoint()
        with Transport.connect(endpoint.ws_url) as transport, Session.create(transport) as session:
            session.open("http://sleight-proxy-test.invalid/page")
            for _ in range(40):
                if session.eval("document.documentElement.dataset.sleightExtension") == "loaded":
                    break
                time.sleep(.25)
            assert session.title() == "local proxy fixture"
            assert session.eval("document.documentElement.dataset.sleightExtension") == "loaded"
            preview = native.preview(profile["id"], session.target_id)
            assert preview["image"].startswith("data:image/jpeg;base64,")
            assert preview["target"] == session.target_id
            # Chromium's inventory sees content-script-only MV3 extensions too.
            session.open("chrome://extensions/")
            inventory = session.eval("new Promise(resolve => chrome.developerPrivate.getExtensionsInfo({includeDisabled:true}, resolve))")
            assert any(p["name"] == "Sleight integration probe" and p["state"] == "ENABLED" for p in inventory)
        assert any("sleight-proxy-test.invalid/page" in p for p in requests)
        native.stop(profile["id"])
        assert native.get(profile["id"])["status"] == "stopped"
        native.delete(profile["id"], profile["name"])
        assert not Path(profile["profile_dir"]).exists() and native.list() == []
    finally:
        native.close()
        server.shutdown()
        server.server_close()
