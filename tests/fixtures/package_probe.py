"""Smoke a clean wheel install and one actual `sleight ui` process."""

import importlib.metadata
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
import psutil

import sleight
from sleight.core.static import parse_html
from sleight.deploy.api.app import INDEX

assert Path(sleight.__file__).resolve().is_relative_to(Path(sys.prefix).resolve()), sleight.__file__
assert not importlib.metadata.metadata("sleight").get_all("Provides-Extra")
assert parse_html("<p>installed</p>", xpath=True).xpath("//p/text()") == ["installed"]
assert INDEX.is_file() and list(INDEX.parent.glob("assets/*.js"))
sock = socket.socket()
sock.bind(("127.0.0.1", 0))
port = sock.getsockname()[1]
sock.close()
with tempfile.TemporaryDirectory(prefix="sleight-wheel-") as root:
    env = {**os.environ, "SLEIGHT_HOME": root, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"}
    for key in ("SLEIGHT_DATABASE_URL", "SLEIGHT_UI_TOKEN", "SLEIGHT_UPSTREAM", "SLEIGHT_TOKEN", "PYTHONPATH"):
        env.pop(key, None)
    log = Path(root) / "server.log"
    command = [sys.executable, "-c", "from sleight.cli import main; raise SystemExit(main())", "ui", "--port", str(port)]
    def stop(process):
        # Windows venv launchers can own a separate Python interpreter process.
        children = psutil.Process(process.pid).children(recursive=True)
        for child in children:
            child.terminate()
        process.terminate()
        psutil.wait_procs(children, timeout=10)
        process.wait(timeout=10)
    def start():
        output = log.open("wb")
        process = subprocess.Popen(command, cwd=root, env=env, stdout=output, stderr=output)
        output.close()
        for _ in range(150):
            try:
                if httpx.get(f"http://127.0.0.1:{port}/", trust_env=False).status_code == 200:
                    return process
            except httpx.HTTPError:
                pass
            if process.poll() is not None:
                raise AssertionError(log.read_text(encoding="utf-8", errors="replace"))
            time.sleep(.1)
        stop(process)
        raise AssertionError("UI startup timed out")
    process = start()
    try:
        text = log.read_text(encoding="utf-8")
        token = re.search(r"首次管理员 token.*：(sl_\S+)", text)[1]
        headers = {"X-Sleight-Token": token}
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False) as client:
            assert client.get("/api/auth/me").status_code == 401
            assert client.get("/api/auth/me", headers=headers).json()["storage"] == "sqlite"
            assert client.get("/api/v1/fleet", headers=headers).json()["counts"]["all"] == 0
            assert not client.get("/api/v1/settings/alerts", headers=headers).json()["configured"]
            assert client.get("/api/v1/settings/storage", headers=headers).json()["current"]["backend"] == "sqlite"
            assert len(client.get("/api/v1/launch-templates", headers=headers).json()) >= 5
            page = client.get("/")
            for asset in re.findall(r'(?:src|href)="\./(assets/[^\"]+)"', page.text):
                assert client.get("/" + asset).status_code == 200
            response = client.post("/mcp", headers={**headers, "Accept": "application/json, text/event-stream"},
                                  json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                                      "protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "wheel", "version": "1"}}})
            assert response.status_code == 200, response.text
            assert json.loads(response.text)["result"]["serverInfo"]["name"] == "sleight"
            response = client.post("/mcp", headers={**headers, "Accept": "application/json, text/event-stream"},
                                   json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
            assert response.status_code == 200, response.text
            tools = {tool["name"]: tool for tool in response.json()["result"]["tools"]}
            assert tools["browser_fetch"]["inputSchema"]["properties"]["method"]["enum"] == ["GET", "HEAD"]
            assert "referrer" in tools["browser_session"]["inputSchema"]["properties"]
    finally:
        stop(process)
    process = start()
    try:
        assert "首次管理员 token" not in log.read_text(encoding="utf-8")
        assert httpx.get(f"http://127.0.0.1:{port}/api/auth/me", headers=headers, trust_env=False).status_code == 200
    finally:
        stop(process)
print("Clean wheel: one Python UI service, bundled assets, XPath, authenticated API/MCP and restart passed")
