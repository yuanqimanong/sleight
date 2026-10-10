import time

from fastapi.testclient import TestClient

from sleight.deploy.api.app import create_app
from sleight.deploy.api.jobs import Job, Jobs
from sleight.deploy.render import merge_compose
from sleight.deploy.spec import DeploySpec


def test_cookie_login_no_url_secret_and_cross_site_guard(tmp_path, monkeypatch):
    monkeypatch.setenv("SLEIGHT_HOME", str(tmp_path))
    with TestClient(create_app(token="private", runtime=False)) as client:
        assert client.get("/").status_code == 200
        assert client.get("/api/hosts?token=private").status_code == 401
        assert client.post("/api/auth/login", json={"token": "private"}).status_code == 200
        assert client.get("/api/hosts").status_code == 200
        assert client.post("/api/hosts", json={"name": "bad"}, headers={"Origin": "https://other.test"}).status_code == 403
        client.post("/api/auth/logout")
        assert client.get("/api/hosts").status_code == 401


def test_jobs_survive_restart_with_interruption(tmp_path):
    jobs = Jobs(tmp_path / "jobs.db")
    unfinished = Job("one", "deploy", "local")
    jobs._save(unfinished)
    restored = Jobs(tmp_path / "jobs.db")
    assert restored.get("one").status == "interrupted"
    job = restored.start("probe", "local", lambda say: (say("ready"), {"ok": True})[1])
    for _ in range(100):
        if job.finished:
            break
        time.sleep(.01)
    again = Jobs(tmp_path / "jobs.db")
    assert again.get(job.id).status == "ok"
    assert again.get(job.id).lines == ["ready"]


def test_compose_preserves_custom_network_mounts_and_variables():
    import yaml

    source = """services:
  manager:
    image: old:v1
    networks: [lan]
    volumes: [/srv/data:/data, /srv/plugins:/data/extensions:ro]
    environment: {AUTH_TOKEN: '${AUTH_TOKEN}', CUSTOM: keep}
networks:
  lan: {external: true}
"""
    rendered = merge_compose(source, DeploySpec())
    body = yaml.safe_load(rendered)
    service = body["services"]["manager"]
    assert service["networks"] == ["lan"]
    assert service["volumes"] == ["/srv/data:/data", "/srv/plugins:/data/extensions:ro"]
    assert service["environment"]["CUSTOM"] == "keep"
    assert service["mem_limit"] == "4gb"
    assert body["networks"]["lan"]["external"] is True
    assert merge_compose(rendered, DeploySpec()) == rendered


def test_proxy_prefix_requires_service_auth_and_works_for_cookie_path(tmp_path, monkeypatch):
    monkeypatch.setenv("SLEIGHT_HOME", str(tmp_path))
    with TestClient(create_app(token="private", runtime=False)) as client:
        response = client.get("/api/defaults", headers={"X-Sleight-Token": "private", "X-Forwarded-Prefix": "/sleight"})
        assert response.status_code == 200
        assert response.json()["version"].startswith("0.6")
        assert "token=private" not in client.get("/").text


def test_mounted_assets_are_served_behind_trusted_nested_prefix(tmp_path, monkeypatch):
    from sleight.deploy.api.app import INDEX

    monkeypatch.setenv("SLEIGHT_HOME", str(tmp_path))
    with TestClient(create_app(token="private", runtime=False)) as client:
        for prefix in ("/sleight", "/sleight/agent/local-test"):
            for asset in (INDEX.parent / "assets").iterdir():
                response = client.get("/assets/" + asset.name,
                                      headers={"X-Sleight-Token": "private", "X-Forwarded-Prefix": prefix})
                assert response.status_code == 200
                assert response.content == asset.read_bytes()


def test_template_clear_proxy_keeps_paths_and_dynamic_task_fields(tmp_path, monkeypatch):
    from sleight.runtime.config import Templates

    monkeypatch.setenv("SLEIGHT_HOME", str(tmp_path))
    templates = Templates()
    row = templates.save({"name": "first", "proxy": "http://user:secret@localhost:8080", "extension_paths": ["/data/a"]})
    assert "secret" not in row["proxy_display"]
    updated = templates.save({"id": row["id"], "name": "direct", "proxy": "", "extension_paths": ["/data/a"]})
    assert updated["proxy_display"] == ""
    body = templates.apply({"template_id": row["id"], "fingerprint_seed": 100, "proxy": "http://sid@localhost:8000"})
    assert body["fingerprint_seed"] == 100 and body["extension_paths"] == ["/data/a"]
    assert body["proxy"] == "http://sid@localhost:8000"


def test_sse_resumes_after_last_event_id_and_caps_retained_logs(tmp_path, monkeypatch):
    monkeypatch.setenv("SLEIGHT_HOME", str(tmp_path))
    app = create_app(runtime=False)
    with TestClient(app) as client:
        job = app.state.jobs.start("log-probe", "local", lambda say: [say(str(n)) for n in range(1003)])
        for _ in range(3000):
            if job.finished:
                break
            time.sleep(.01)
        assert job.finished and len(job.lines) == 1000 and job.log_offset == 3
        response = client.get(f"/api/jobs/{job.id}/events?after=1000", headers={"Last-Event-ID": "1001"})
        assert response.status_code == 200
        assert '"cursor": 1001' not in response.text
        assert '"cursor": 1002' in response.text and '"cursor": 1003' in response.text
        assert "event: done" in response.text


def test_audit_retains_status_without_body_token_or_query(tmp_path, monkeypatch):
    from sleight.deploy.store import Store

    monkeypatch.setenv("SLEIGHT_HOME", str(tmp_path))
    with TestClient(create_app(token="secret", runtime=False)) as client:
        client.post("/api/auth/login?private=query-secret", json={"token": "body-secret"})
    rows = Store().events(host="console")
    assert len(rows) == 1 and rows[0].ok is False
    assert rows[0].detail == "user=anonymous POST /api/auth/login => 401"
