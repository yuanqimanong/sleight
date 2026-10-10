"""Console contracts, notifications and storage changes on isolated stores."""

import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from sleight.core import errors
from sleight.deploy.api.app import create_app
from sleight.service.database import Database
from sleight.service.execution import Controller, Supervisor
from sleight.service.fleet import Fleet
from sleight.service.identity import Identity, Vault
from sleight.service.monitoring import Monitor
from sleight.service.storage_config import StorageSettings, configured_url


def owner():
    identity = Identity()
    identity.ensure_admin("console-fixture")
    return identity.lookup("console-fixture")


def profile(db, pid="profile", source="source", **extra):
    row = {"id": pid, "owner": "admin", "environment": "native", "source_id": source, "name": pid,
           "ephemeral": False, "state": "idle", "created": time.time(), "touched": time.time(), **extra}
    db.put("profiles", pid, row)
    return row


def test_catalogue_deduplicates_alias_and_preserves_unknown(service_db, monkeypatch):
    user = owner()
    profile(service_db)
    profile(service_db, "alias")
    profile(service_db, "unreachable", "gone", ephemeral=True, state="cleanup_pending")
    monkeypatch.setattr("sleight.service.fleet.NativeSupervisor", lambda: SimpleNamespace(list=lambda: [{"id": "source", "status": "running", "name": "old", "port": 123, "memory_bytes": 2048}]))
    data = Fleet(Supervisor()).catalogue(user)
    assert data["counts"] == {"all": 2, "running": 1, "persistent": 1, "temporary": 1}
    assert next(r for r in data["instances"] if r["id"] == "gone")["status"] == "cleanup_pending"
    operator = Identity().save_user({"name": "other"})
    principal = Identity().lookup(Identity().issue(operator["id"])["token"])
    assert Fleet(Supervisor()).catalogue(principal)["instances"] == []


def test_legacy_binding_is_atomic(service_db):
    user = owner()
    fleet = Fleet(Supervisor())
    legacy = {"environment": "native", "source_id": "shared", "owner": user.user_id, "name": "legacy", "ephemeral": False}
    with ThreadPoolExecutor(max_workers=6) as workers:
        bound = list(workers.map(lambda _: fleet.bind(legacy, user), range(6)))
    assert len({r["id"] for r in bound}) == 1
    assert len(service_db.list("profiles")) == 1


def test_instance_logs_keep_latest_200_and_enforce_ownership(service_db):
    user = owner()
    profile(service_db)
    fleet = Fleet(Supervisor())
    for index in range(205):
        fleet.record("profile", "start", f"entry {index}")
    entries = fleet.logs("profile:profile", user)["entries"]
    assert len(entries) == 200
    assert entries[0]["message"] == "entry 5"
    assert entries[-1]["message"] == "entry 204"
    operator = Identity().save_user({"name": "other"})
    principal = Identity().lookup(Identity().issue(operator["id"])["token"])
    with pytest.raises(errors.NotFound):
        fleet.logs("profile:profile", principal)


def test_temporary_stop_counts_only_after_cleanup(service_db, monkeypatch):
    user, supervisor = owner(), Supervisor()
    row = profile(service_db, ephemeral=True)
    fleet = Fleet(supervisor)
    def fail(*a, **kw):
        raise errors.InstanceError("cannot confirm cleanup")
    monkeypatch.setattr(supervisor.controller, "cleanup", fail)
    with pytest.raises(errors.InstanceError):
        fleet.action("profile:profile", "stop", user)
    assert service_db.get("profiles", row["id"])
    with pytest.raises(ValueError, match="不支持重启"):
        fleet.action("profile:profile", "restart", user)
    monkeypatch.setattr(supervisor.controller, "cleanup", lambda r, **kw: service_db.delete("profiles", r["id"]) if kw["delete"] else None)
    assert fleet.action("profile:profile", "stop", user)["deleted"]
    assert service_db.get("profiles", row["id"]) is None


def test_name_reservation_is_global_and_idempotent(service_db, monkeypatch):
    from sleight.service import execution
    user, controller = owner(), Controller()
    monkeypatch.setattr(execution, "NativeSupervisor", lambda: SimpleNamespace(create=lambda body: {"id": body["name"]}))
    def create(index):
        try:
            return controller.create({"name": "Unique", "binary": "fixture", "ephemeral": False}, user, str(index))
        except errors.Busy:
            return None
    with ThreadPoolExecutor(max_workers=5) as pool:
        results = list(pool.map(create, range(5)))
    assert len([r for r in results if r]) == 1
    request = next(str(i) for i, r in enumerate(results) if r)
    assert controller.create({"name": "Unique"}, user, request)["id"] == next(r for r in results if r)["id"]


def test_alert_duration_recovery_and_failure_backoff(service_db):
    owner()
    sent, tick = [], [0]
    monitor = Monitor(clock=lambda: tick[0], sender=sent.append)
    monitor.save({"webhook": "https://open.feishu.cn/open-apis/bot/v2/hook/fixture", "duration_s": 10, "cooldown_s": 60})
    def sample(cpu):
        monitor.evaluate({"host": {"cpu_percent": cpu, "memory_percent": 20, "name": "fixture"}, "containers": []})
    sample(99)
    tick[0] = 5
    sample(99)
    assert sent == []
    tick[0] = 10
    sample(99)
    assert len(sent) == 1
    tick[0] = 15
    sample(99)
    assert len(sent) == 1
    tick[0] = 20
    sample(20)
    assert len(sent) == 2 and "恢复" in sent[-1]
    failures = []
    def sender(message):
        failures.append(message)
        raise ValueError("fixture network failure")
    monitor.sender = sender
    for moment in (25, 30, 35, 40, 45):
        tick[0] = moment
        sample(99)
    assert len(failures) == 1
    tick[0] = 50
    sample(99)
    tick[0] = 55
    sample(99)
    tick[0] = 60
    sample(99)
    tick[0] = 65
    sample(99)
    assert len(failures) == 2
    assert "fixture" not in str(service_db.get("settings", "alerts"))
    assert Vault().get("alert-webhook").endswith("fixture")
    monitor.save({"webhook": ""})
    tick[0] = 100
    sample(99)
    assert len(failures) == 2


def test_missing_samples_reset_sustained_evidence(service_db):
    owner()
    sent, tick = [], [100]
    monitor = Monitor(clock=lambda: tick[0], sender=sent.append)
    monitor.save({"webhook": "https://open.larksuite.com/open-apis/bot/v2/hook/fixture", "duration_s": 10})
    value = {"host": {"name": "host", "cpu_percent": 99, "memory_percent": 20}, "containers": []}
    monitor.evaluate(value)
    tick[0] = 200
    monitor.evaluate(value)
    assert not sent


def test_empty_sqlite_migration_freezes_until_restart(service_db, tmp_path, monkeypatch):
    identity = Identity()
    identity.ensure_admin("keep-token")
    controller = Controller()
    monkeypatch.delenv("SLEIGHT_DATABASE_URL", raising=False)
    settings = StorageSettings(service_db)
    target_url = "sqlite:///" + str(tmp_path / "target.db")
    monkeypatch.setattr(settings, "connection", lambda body: target_url)
    result = settings.migrate({"backend": "sqlite"})
    assert result["restart_required"] and service_db.storage_pending
    with pytest.raises(errors.Busy):
        service_db.put("settings", "late-change", True)
    with pytest.raises(errors.Busy):
        controller.create({}, identity.lookup("keep-token"), "late")
    assert configured_url(service_db.root) == target_url
    assert "keep-token" not in (service_db.root / "storage.json").read_text()
    with service_db.state("identity") as read:
        assert read.get("tokens")
    target = Database(target_url, import_legacy=False)
    try:
        with target.state("identity") as restored:
            assert restored == read
        assert target.get("settings", "late-change") is None
    finally:
        target.engine.dispose()


def test_migration_rejects_running_and_business_tables(service_db, tmp_path, monkeypatch):
    owner()
    monkeypatch.delenv("SLEIGHT_DATABASE_URL", raising=False)
    settings = StorageSettings(service_db)
    url = "sqlite:///" + str(tmp_path / "business.db")
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE business (id INTEGER)"))
    monkeypatch.setattr(settings, "connection", lambda body: url)
    with pytest.raises(ValueError, match="目标包含"):
        settings.migrate({"backend": "sqlite"})
    assert not service_db.storage_pending and not (service_db.root / "storage.json").exists()
    engine.dispose()
    url = "sqlite:///" + str(tmp_path / "empty.db")
    with service_db.state("service-sessions") as state:
        state["records"]["running"] = {"state": "running"}
    with pytest.raises(ValueError, match="停止浏览器"):
        settings.migrate({"backend": "sqlite"})
    assert not service_db.storage_pending
    service_db.put("settings", "still-usable", True)


def test_console_scope_and_template_defaults(service_db):
    from sleight.service.launch_templates import LaunchTemplates
    owner()
    configs = [r for r in LaunchTemplates().list() if r["kind"] == "cloak"]
    assert [r["name"] for r in configs] == ["Win-US-00", "Win-US-01", "Win-US-02"]
    assert all("proxy" not in r["config"] for r in configs)
    operator = Identity().save_user({"name": "operator"})
    token = Identity().issue(operator["id"])["token"]
    with TestClient(create_app(token="console-fixture", runtime=False)) as client:
        headers = {"X-Sleight-Token": token}
        assert client.get("/api/v1/settings/storage", headers=headers).status_code == 403
        assert client.post("/api/v1/launch-templates", headers=headers, json={}).status_code == 403
        assert client.get("/api/v1/fleet", headers=headers).json()["counts"]["all"] == 0
        assert all("binary" not in r for r in client.get("/api/v1/kernels", headers=headers).json())
