"""Opt-in localhost agent HTTP/SSE and official VNC round trip."""
import asyncio
import os
import re
import uuid

import httpx
import pytest
import websockets

pytestmark = [pytest.mark.manager, pytest.mark.skipif(os.environ.get("SLEIGHT_LOCAL_DOCKER_TEST") != "1", reason="Dedicated local Sleight UI required")]


def test_agent_proxy_http_sse_extension_inventory_and_vnc():
    with httpx.Client(base_url="http://127.0.0.1:18700", headers={"X-Sleight-Token": "sleight-v06-ui"}, timeout=180) as client:
        agent = client.post("/api/agents", json={"name": "loopback-contract", "url": "http://127.0.0.1:18700", "token": "sleight-v06-ui"})
        agent.raise_for_status()
        aid = agent.json()["id"]
        prefix = f"/agent/{aid}"
        profile = None
        try:
            page = client.get(prefix + "/")
            assert page.status_code == 200 and "assets/" in page.text
            assert "sleight-v06-ui" not in page.text
            for asset in re.findall(r'(?:src|href)="\./(assets/[^\"]+)"', page.text):
                assert client.get(prefix + "/" + asset).status_code == 200
            assert client.get(prefix + "/api/runtime/platform").json()["os"] == "Windows"
            assert client.get(prefix + "/api/agents").status_code == 403
            path = prefix + "/api/hosts/local/"
            response = client.post(path + "profiles?deployment=test", json={"name": "agent-" + uuid.uuid4().hex,
                                 "fingerprint_seed": 1234, "extension_paths": ["/data/extensions/integration-probe"]})
            response.raise_for_status()
            profile = response.json()
            pid = profile["id"]
            client.post(path + f"profiles/{pid}/launch?deployment=test").raise_for_status()
            dashboard = client.get(prefix + "/viewer/local/test/")
            assert dashboard.status_code == 200 and prefix + "/viewer/local/test" in dashboard.text

            async def check_vnc():
                url = f"ws://127.0.0.1:18700{prefix}/viewer/local/test/api/profiles/{pid}/vnc"
                async with websockets.connect(url, additional_headers={"X-Sleight-Token": "sleight-v06-ui"},
                                              origin="http://127.0.0.1:18700", subprotocols=["binary"], open_timeout=30) as socket:
                    assert (await asyncio.wait_for(socket.recv(), 15)).startswith(b"RFB ")
            asyncio.run(check_vnc())
            job = client.post(path + "extensions/verify?deployment=test", json={"launch": False, "settle": 1}).json()["job"]
            events = client.get(prefix + f"/api/jobs/{job}/events")
            assert events.status_code == 200 and "event: done" in events.text
            report = client.get(prefix + f"/api/jobs/{job}").json()
            assert report["status"] == "ok", report
            assert any(row["id"] == pid and row["ok"] and row["expected"] == 1 for row in report["result"]), report
        finally:
            if profile:
                client.delete(f"/api/hosts/local/profiles/{profile['id']}?deployment=test&confirm={profile['name']}").raise_for_status()
            client.delete(f"/api/agents/{aid}").raise_for_status()
