"""Explicitly enabled, local-only Docker contract and failure tests."""

import os
import subprocess
import sys
import time
import uuid

import pytest

from sleight.core.errors import Busy
from sleight.providers import CloakBrowserManager, ProfileSpec
from sleight.runtime import Governor, SQLiteLedger

pytestmark = pytest.mark.manager
BASE = "http://127.0.0.1:19065"


@pytest.fixture
def manager():
    if os.environ.get("SLEIGHT_LOCAL_DOCKER_TEST") != "1":
        pytest.skip("Set SLEIGHT_LOCAL_DOCKER_TEST=1 for dedicated local containers")
    governor = Governor(resource="v06-contract-" + uuid.uuid4().hex, limit=1,
                        ledger=SQLiteLedger())
    mgr = CloakBrowserManager(BASE, token="sleight-local-test-manager", governor=governor)
    yield mgr
    for row in mgr.list_profiles():
        if row.get("name", "").startswith(governor.resource):
            mgr.delete_profile(row["id"], force=True)
    governor.close()


def test_official_schema_cdp_and_capacity(manager):
    assert manager.capabilities()["generation"] == "modern"
    name = manager.governor.resource
    first = manager.create_profile(ProfileSpec(name=name + "-first", tags=("ephemeral",), fingerprint_seed=1234), launch=True)
    second = manager.create_profile(ProfileSpec(name=name + "-second", tags=("ephemeral",)))
    with pytest.raises(Busy):
        manager.ensure_ready(second.id)
    with manager.lease(instance_id=first.id) as handle, handle.session() as session:
        session.open("data:text/html,<title>v06 CDP</title><h1>ready</h1>")
        assert session.title() == "v06 CDP"
        assert session.eval("document.querySelector('h1').textContent") == "ready"
    manager.delete_profile(first.id, force=True)
    manager.governor.collect(manager)


def test_official_extension_has_real_content_script_effect(manager):
    url = os.environ.get("SLEIGHT_FIXTURE_URL")
    if not url:
        pytest.skip("Needs local fixture HTTP server accessible from Docker")
    profile = manager.create_profile(ProfileSpec(name=manager.governor.resource + "-extension", tags=("ephemeral",),
                                               extension_paths=("/tmp/sleight-test-extension",)), launch=True)
    with manager.lease(instance_id=profile.id) as handle, handle.session() as session:
        session.open(url)
        for _ in range(40):
            if session.eval("document.documentElement.dataset.sleightExtension") == "loaded":
                break
            time.sleep(.25)
        assert session.eval("document.documentElement.dataset.sleightExtension") == "loaded"


def test_browser_exit_reclaims_playwright_driver(manager):
    docker = r"C:\Program Files\Docker\Docker\resources\bin\docker.exe"

    def drivers():
        result = subprocess.run([docker, "exec", "sleight06-manager", "sh", "-c",
                                 "ps -eo args | grep '[p]laywright/driver/node' | wc -l"],
                                capture_output=True, text=True, check=True)
        return int(result.stdout.strip())

    baseline = drivers()
    profile = manager.create_profile(ProfileSpec(name=manager.governor.resource + "-crash", tags=("ephemeral",)), launch=True)
    assert drivers() >= baseline + 1
    with manager.lease(instance_id=profile.id) as handle:
        transport = handle.transport
        try:
            transport.call("Browser.close")
        except Exception:
            pass
        finally:
            transport.close()
    for _ in range(80):
        if drivers() == baseline:
            break
        time.sleep(.25)
    assert drivers() == baseline, "Playwright driver survived browser exit"


def test_hard_killed_client_is_reaped_without_freeing_failed_cleanup(manager, tmp_path):
    import json

    ready = tmp_path / "worker.json"
    resource = manager.governor.resource
    worker = """import json,sys,time
from sleight.providers import CloakBrowserManager,ProfileSpec
from sleight.runtime import Governor,SQLiteLedger
g=Governor(resource=sys.argv[1],limit=1,ttl=3,ledger=SQLiteLedger())
m=CloakBrowserManager('http://127.0.0.1:19065',token='sleight-local-test-manager',governor=g)
p=m.create_profile(ProfileSpec(name=sys.argv[1]+'-killed',tags=('ephemeral',)),launch=True)
with open(sys.argv[2],'w') as f: json.dump({'id':p.id},f)
while True: time.sleep(1)
"""
    process = subprocess.Popen([sys.executable, "-c", worker, resource, str(ready)])
    try:
        for _ in range(360):
            if ready.exists():
                break
            assert process.poll() is None
            time.sleep(.25)
        assert ready.exists(), "worker did not finish starting"
        pid = json.loads(ready.read_text())["id"]
        assert manager.governor.collect(manager) == []  # active owner is protected
        process.kill()
        process.wait(timeout=10)
        time.sleep(3.5)
        original = manager.delete_profile

        def offline(*args, **kwargs):
            raise ConnectionError("simulated cleanup outage")

        manager.delete_profile = offline
        assert manager.governor.collect(manager) == []
        assert manager.governor.snapshot()["records"][pid]["slot"]
        manager.delete_profile = original
        with manager.governor.ledger.transaction(resource) as state:
            state["records"][pid]["retry_at"] = 0
        assert manager.governor.collect(manager) == [pid]
        assert pid not in {p["id"] for p in manager.list_profiles()}
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)
