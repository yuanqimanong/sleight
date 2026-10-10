from concurrent.futures import ThreadPoolExecutor

import pytest

from sleight.core.errors import Busy, InstanceError, NotFound
from sleight.core.types import InstanceStatus
from sleight.providers.compat import adapt_payload
from sleight.runtime import Governor, SQLiteLedger


class Manager:
    def __init__(self):
        self.rows = {}
        self.fail = False

    def list_profiles(self):
        return list(self.rows.values())

    def status(self, pid):
        if pid not in self.rows:
            return InstanceStatus.NOT_FOUND
        return InstanceStatus.RUNNING if self.rows[pid]["status"] == "running" else InstanceStatus.STOPPED

    def stop(self, pid):
        if self.fail:
            raise InstanceError("offline")
        self.rows[pid]["status"] = "stopped"

    def delete_profile(self, pid, *, force):
        if self.fail:
            raise InstanceError("offline")
        if pid not in self.rows:
            raise NotFound(pid)
        del self.rows[pid]


@pytest.fixture
def ledger(tmp_path):
    return SQLiteLedger(tmp_path / "ledger.db")


def test_concurrent_admission_counts_starting_and_external(ledger):
    manager = Manager()
    manager.rows["external"] = {"id": "external", "status": "running"}
    governors = [Governor(resource="test", limit=3, ledger=ledger, heartbeat=False) for _ in range(8)]

    def reserve(i):
        try:
            governors[i].reserve(manager, str(i))
            return True
        except Busy:
            return False

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(reserve, range(8))) == 2
    assert sum(r["slot"] for r in governors[0].snapshot()["records"].values()) == 2


def test_expiration_cleanup_retry_keeps_slot_and_persistent_data(ledger):
    owner = Governor(resource="test", limit=1, ledger=ledger, heartbeat=False)
    collector = Governor(resource="test", limit=1, ledger=ledger, heartbeat=False)
    manager = Manager()
    manager.rows["temp"] = {"id": "temp", "status": "stopped", "tags": []}
    owner.created("temp", ephemeral=True)
    owner.reserve(manager, "temp")
    manager.rows["persistent"] = {"id": "persistent", "status": "stopped", "tags": []}
    owner.created("persistent", ephemeral=False)
    with ledger.transaction("test") as state:
        state["owners"][owner.owner] = 0
    manager.fail = True
    assert collector.collect(manager) == []
    assert collector.snapshot()["records"]["temp"]["slot"]
    with pytest.raises(Busy):
        collector.reserve(manager, "persistent")
    with ledger.transaction("test") as state:
        state["records"]["temp"]["retry_at"] = 0
    manager.fail = False
    assert collector.collect(manager) == ["temp"]
    assert "persistent" in manager.rows
    assert "temp" not in collector.snapshot()["records"]


def test_live_owner_and_lost_create_response(ledger):
    owner = Governor(resource="test", ledger=ledger, heartbeat=False)
    collector = Governor(resource="test", ledger=ledger, heartbeat=False)
    manager = Manager()
    manager.rows["lost"] = {"id": "lost", "status": "running",
                            "tags": [{"tag": "ephemeral"}, {"tag": owner.creation_tags()[0]}]}
    assert collector.collect(manager) == []
    with ledger.transaction("test") as state:
        state["owners"][owner.owner] = 0
    assert collector.collect(manager) == ["lost"]


def test_persistent_failed_start_stops_without_deleting(ledger):
    owner = Governor(resource="test", ledger=ledger, heartbeat=False)
    manager = Manager()
    manager.rows["saved"] = {"id": "saved", "status": "running", "tags": []}
    owner.reserve(manager, "saved")
    owner.request_cleanup("saved", failed_start=True)
    assert owner.collect(manager) == ["saved"]
    assert manager.rows["saved"]["status"] == "stopped"


def test_modern_partial_update_preserves_unmentioned_fields():
    assert adapt_payload({"proxy": "http://localhost:8000"}, modern=True) == {"proxy": "http://localhost:8000"}
    assert adapt_payload({"extension_paths": []}, modern=True) == {"extension_paths": []}
    assert adapt_payload({"gpu_family": "intel"}, modern=True) == {"gpu_family": "intel"}
    modern = adapt_payload({"launch_args": ["--load-extension=/data/a,/data/b", "--foo"],
                            "gpu_vendor": "NVIDIA", "platform": "windows"}, modern=True)
    assert modern == {"launch_args": ["--foo"], "extension_paths": ["/data/a", "/data/b"], "gpu_family": "nvidia"}
    with pytest.raises(ValueError):
        adapt_payload({"platform": "linux"}, modern=True)
    with pytest.raises(ValueError):
        adapt_payload({"restore_session": True}, modern=False)


def test_capacity_mismatch_and_ephemeral_ownership_are_rejected(ledger):
    owner = Governor(resource="strict", limit=2, ledger=ledger, heartbeat=False)
    with pytest.raises(InstanceError, match="same limit"):
        Governor(resource="strict", limit=3, ledger=ledger, heartbeat=False)
    other = Governor(resource="strict", limit=2, ledger=ledger, heartbeat=False)
    owner.created("owned", ephemeral=True)
    with pytest.raises(Busy, match="owns"):
        other.reserve(Manager(), "owned")
