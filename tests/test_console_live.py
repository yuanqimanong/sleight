"""Real native tabs and actual PostgreSQL bootstrap migrations; local opt-in."""

import json
import os
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from sleight.deploy.api.app import create_app
from sleight.runtime.native import discover_browsers
from sleight.service.database import Database, _databases
from sleight.service.identity import Identity
from sleight.service.storage_config import StorageSettings, configured_url

pytestmark = pytest.mark.skipif(os.environ.get("SLEIGHT_CONSOLE_LIVE") != "1", reason="Dedicated local console integration")


def test_real_chrome_switch_tabs_navigate_restart_and_session_logs(tmp_path, monkeypatch):
    monkeypatch.setenv("SLEIGHT_HOME", str(tmp_path))
    monkeypatch.delenv("SLEIGHT_DATABASE_URL", raising=False)
    browsers = discover_browsers()
    if not browsers:
        pytest.skip("No installed native browser")
    app = create_app(token="console-test", runtime=False)
    headers = {"X-Sleight-Token": "console-test"}
    with TestClient(app) as web:
        r = web.post("/api/v1/fleet/create", headers=headers, json={"name": "native-tab-fixture", "ephemeral": False, "binary": browsers[0]["binary"], "headless": True})
        assert r.status_code == 200, r.text
        pid, ref = r.json()["id"], "profile:" + r.json()["id"]
        def action(operation):
            r = web.post("/api/v1/fleet/action", headers=headers, json={"ref": ref, "action": operation})
            assert r.status_code == 200, r.text
            return r.json()
        try:
            action("start")
            targets = []
            for mode in ("tab", "tab", "window"):
                r = web.post("/api/v1/fleet/navigate", headers=headers, json={"ref": ref, "url": "about:blank", "mode": mode})
                assert r.status_code == 200, r.text
                targets.append(r.json()["target"])
            for i in range(15):
                selected = targets[i % len(targets)]
                result = web.get("/api/v1/fleet/inspect", headers=headers, params={"ref": ref, "target": selected})
                assert result.status_code == 200, result.text
                assert result.json()["target"] == selected
                assert result.json()["image"].startswith("data:image/jpeg;base64,")
                assert set(targets) <= {p["targetId"] for p in result.json()["pages"]}
            stale = web.get("/api/v1/fleet/inspect", headers=headers, params={"ref": ref, "target": "closed-tab"})
            assert stale.status_code == 200 and stale.json()["target"]
            assert web.get("/api/v1/fleet", headers=headers).json()["counts"]["running"] == 1
            action("restart")
            logs = web.get("/api/v1/fleet/logs", headers=headers, params={"ref": ref}).json()["entries"]
            assert any(r["action"] == "released" for r in logs)
            assert any(r["action"] == "running" for r in logs)
            action("stop")
            assert web.get("/api/v1/profiles/" + pid, headers=headers).status_code == 200
            assert web.get("/api/v1/fleet", headers=headers).json()["counts"]["running"] == 0
            assert web.post("/api/v1/fleet/delete", headers=headers, json={"ref": ref, "confirm": "native-tab-fixture"}).status_code == 200
        finally:
            app.state.supervisor.shutdown()


def test_sqlite_pg_sqlite_bootstrap_preserves_tokens_and_templates(tmp_path, monkeypatch):
    url = os.environ.get("SLEIGHT_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Dedicated local PostgreSQL URL is required")
    monkeypatch.setenv("SLEIGHT_HOME", str(tmp_path))
    monkeypatch.delenv("SLEIGHT_DATABASE_URL", raising=False)
    engine = create_engine(url.replace("postgresql://", "postgresql+psycopg://", 1))
    schema = "console_" + uuid.uuid4().hex
    with engine.begin() as conn:
        conn.execute(text(f"CREATE SCHEMA {schema}"))
    db = Database()
    from sleight.service.database import database
    identity = Identity()
    identity.ensure_admin("round-trip-token")
    db.put("launch-templates", "custom", {"id": "custom", "name": "custom", "kind": "cloak", "config": {}})
    parsed = __import__("sqlalchemy").engine.make_url(url)
    body = {"backend": "postgresql", "host": parsed.host, "port": parsed.port, "username": parsed.username,
            "password": parsed.password, "database": parsed.database, "schema": schema, "sslmode": "disable"}
    try:
        # Use the same cached database as Identity, mirroring a running UI process.
        active = database()
        active.put("launch-templates", "custom", db.get("launch-templates", "custom"))
        status = StorageSettings(active).migrate(body)
        assert status["restart_required"]
        assert parsed.password not in json.dumps(json.loads((tmp_path / "storage.json").read_text()))
        _databases.clear()  # Simulate stopping the process and starting it again.
        pg = database()
        assert pg.engine.dialect.name == "postgresql"
        assert not StorageSettings(pg).status()["restart_required"]
        assert Identity().lookup("round-trip-token") and pg.get("launch-templates", "custom")
        result = StorageSettings(pg).migrate({"backend": "sqlite"})
        assert result["restart_required"] and (tmp_path / "storage.previous.json").is_file()
        pg.engine.dispose()
        _databases.clear()
        local = database()
        assert local.engine.dialect.name == "sqlite" and Identity().lookup("round-trip-token")
        assert local.get("launch-templates", "custom")
        assert configured_url(tmp_path).startswith("sqlite:")
        local.engine.dispose()
    finally:
        db.engine.dispose()
        active.engine.dispose()
        with engine.begin() as conn:
            conn.execute(text(f"DROP SCHEMA {schema} CASCADE"))
        engine.dispose()
