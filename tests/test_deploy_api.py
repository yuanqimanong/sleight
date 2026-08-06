"""Web 界面。

只测这一层自己的东西：鉴权、主机 CRUD、job 生命周期、SSE、以及库异常有没有被翻成
HTTP 4xx 而不是一页 traceback。部署逻辑本身在 test_deploy_engine.py 里测过了，这里把
``Deployer`` 换成替身。
"""

from __future__ import annotations

import json
from typing import ClassVar

import pytest

fastapi = pytest.importorskip("fastapi", reason="需要 pip install \"sleight[ui]\"")
pytest.importorskip("httpx", reason="fastapi 的 TestClient 需要 httpx")

from fastapi.testclient import TestClient  # noqa: E402

from sleight.deploy import spec as spec_mod  # noqa: E402
from sleight.deploy.api import app as app_mod  # noqa: E402
from sleight.deploy.api.app import Jobs, create_app  # noqa: E402
from sleight.deploy.engine import DeployResult, Plan  # noqa: E402
from sleight.deploy.errors import DeployError  # noqa: E402
from sleight.deploy.preflight import Check, CheckLevel  # noqa: E402

SPEC = spec_mod.DeploySpec(dir="/srv/cbm")


class StubDeployer:
    """记下调用，不碰任何机器。"""

    instances: ClassVar[list[StubDeployer]] = []
    fail: ClassVar[BaseException | None] = None

    def __init__(self, spec, runner, *, sudo=False, on_progress=None, dry_run=False) -> None:
        self.spec = spec
        self.runner = runner
        self.say = on_progress or (lambda _m: None)
        self.calls: list[str] = []
        StubDeployer.instances.append(self)

    def _do(self, name):
        self.calls.append(name)
        self.say(f"步骤 {name}")
        if StubDeployer.fail is not None:
            raise StubDeployer.fail

    def apply(self, **kw):
        self._do("apply")
        return DeployResult(self.spec, "t" * 64, True, ["写入 .env"], {"profiles_total": 3})

    def upgrade(self, image, **kw):
        self._do(f"upgrade:{image}")
        return DeployResult(self.spec, "t" * 64, True, [], {"profiles_total": 3})

    def backup(self):
        self._do("backup")
        return f"{self.spec.backups_dir}/cloakbrowser-20260805-000000.tar.gz"

    def plan(self):
        self._do("plan")
        return Plan(
            self.spec,
            [Check("docker", CheckLevel.OK, "engine 27.3.1")],
            {self.spec.env_path: "AUTH_TOKEN=x\n"},
            ["写入 .env"],
            [("docker", "compose", "up", "-d")],
            ["镜像只钉了标签"],
        )

    def status(self):
        self._do("status")
        return {"container": {"status": "running"}, "api": {"profiles_total": 3}}

    def logs(self, tail=200):
        return f"last {tail} lines"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("SLEIGHT_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(app_mod, "Deployer", StubDeployer)
    StubDeployer.instances = []
    StubDeployer.fail = None


@pytest.fixture
def client():
    return TestClient(create_app())


def wait_job(client, job_id, *, token=None):
    """SSE 读到 done 事件为止，返回那个 job。"""
    query = f"?token={token}" if token else ""
    with client.stream("GET", f"/api/jobs/{job_id}/events{query}") as response:
        assert response.status_code == 200
        payload = None
        for line in response.iter_lines():
            if line.startswith("data:"):
                payload = json.loads(line[5:])
        return payload


# --------------------------------------------------------------------------- #
# 基础
# --------------------------------------------------------------------------- #


def test_defaults_exposes_the_spec_defaults(client):
    body = client.get("/api/defaults").json()
    assert body["default_image"] == spec_mod.DEFAULT_IMAGE
    assert body["spec"]["port"] == 9000
    assert body["auth"] is False


def test_index_is_self_contained():
    """CSP 之外的原因：装了 wheel 的机器可能根本没有外网。"""
    html = app_mod.INDEX.read_text(encoding="utf-8")
    assert "<title>sleight deploy</title>" in html
    assert "http://cdn" not in html and "https://cdn" not in html
    assert "<script src=" not in html


# --------------------------------------------------------------------------- #
# 鉴权
# --------------------------------------------------------------------------- #


def test_no_token_means_no_gate():
    assert TestClient(create_app()).get("/api/hosts").status_code == 200


def test_token_is_required_when_configured():
    client = TestClient(create_app(token="s3cret"))
    assert client.get("/api/hosts").status_code == 401
    assert client.get("/api/hosts", headers={"X-Sleight-Token": "wrong"}).status_code == 401
    assert client.get("/api/hosts", headers={"X-Sleight-Token": "s3cret"}).status_code == 200


def test_query_token_works_because_eventsource_cannot_set_headers():
    client = TestClient(create_app(token="s3cret"))
    assert client.get("/api/hosts?token=s3cret").status_code == 200


def test_serve_refuses_a_public_bind_without_a_token():
    """这个界面能在目标机上跑 ssh 和 docker，开着没口令等于开着远程执行。"""
    with pytest.raises(DeployError, match="refusing to listen"):
        app_mod.serve(host="0.0.0.0", port=0)


# --------------------------------------------------------------------------- #
# 主机
# --------------------------------------------------------------------------- #


def test_local_is_always_offered(client):
    hosts = client.get("/api/hosts").json()
    assert hosts[0]["name"] == "local"
    assert hosts[0]["implicit"] is True


def test_add_list_delete(client):
    added = client.post("/api/hosts", json={
        "name": "hk-01", "ssh": "deploy@1.2.3.4", "port": 2222, "sudo": True,
        "deploy": {"dir": "/srv/cbm", "port": 9100},
    })
    assert added.status_code == 200

    names = [h["name"] for h in client.get("/api/hosts").json()]
    assert "hk-01" in names
    entry = next(h for h in client.get("/api/hosts").json() if h["name"] == "hk-01")
    assert entry["sudo"] is True
    # spec 现在挂在部署下面 —— 一台机可以有好几个 Manager
    assert entry["deployments"][0]["spec"]["port"] == 9100
    assert entry["deployments"][0]["name"] == "default"

    assert client.delete("/api/hosts/hk-01").status_code == 200
    assert "hk-01" not in [h["name"] for h in client.get("/api/hosts").json()]


def test_a_nameless_host_is_rejected(client):
    assert client.post("/api/hosts", json={"ssh": "u@h"}).status_code == 400


def test_an_undeployable_spec_is_rejected_before_saving(client):
    response = client.post("/api/hosts", json={
        "name": "bad", "ssh": "u@h", "deploy": {"image": "repo/img:latest"},
    })
    assert response.status_code == 400
    assert "reproducible" in response.json()["detail"]


def test_deleting_an_unknown_host_is_a_400(client):
    assert client.delete("/api/hosts/nope").status_code == 400


def test_an_unknown_host_is_a_404_everywhere(client):
    assert client.get("/api/hosts/nope/status").status_code == 404
    assert client.post("/api/hosts/nope/preflight", json={}).status_code == 404


# --------------------------------------------------------------------------- #
# 同步接口
# --------------------------------------------------------------------------- #


def test_preflight_returns_checks_changes_and_files(client):
    body = client.post("/api/hosts/local/preflight", json={"spec": {"port": 9100}}).json()
    assert body["checks"][0]["name"] == "docker"
    assert body["changes"] == ["写入 .env"]
    assert body["commands"] == [["docker", "compose", "up", "-d"]]
    assert body["warnings"]
    assert "AUTH_TOKEN" in next(iter(body["files"].values()))


def test_spec_overrides_reach_the_deployer(client):
    client.post("/api/hosts/local/preflight", json={"spec": {"port": 9100, "bogus": "x"}})
    assert StubDeployer.instances[-1].spec.port == 9100


def test_status_and_logs(client):
    assert client.get("/api/hosts/local/status").json()["api"]["profiles_total"] == 3
    assert client.get("/api/hosts/local/logs?tail=5").json()["text"] == "last 5 lines"


def test_a_library_error_is_a_400_not_a_500(client, monkeypatch):
    StubDeployer.fail = DeployError("no deployment there")
    response = client.get("/api/hosts/local/status")
    assert response.status_code == 400
    assert "no deployment there" in response.json()["detail"]


# --------------------------------------------------------------------------- #
# job 与 SSE
# --------------------------------------------------------------------------- #


def test_deploy_returns_a_job_and_streams_progress(client):
    job_id = client.post("/api/hosts/local/deploy", json={"spec": {}}).json()["job"]
    job = wait_job(client, job_id)
    assert job["status"] == "ok"
    assert "步骤 apply" in job["lines"]
    assert job["result"]["changed"] is True
    assert job["result"]["token_hint"].endswith("…")
    assert "t" * 64 not in json.dumps(job)          # 完整 token 不进界面


def test_a_failing_job_reports_the_error_not_a_hang(client):
    StubDeployer.fail = DeployError("port 9000 is already in use")
    job_id = client.post("/api/hosts/local/deploy", json={}).json()["job"]
    job = wait_job(client, job_id)
    assert job["status"] == "error"
    assert "port 9000" in job["error"]


def test_upgrade_needs_an_image(client):
    assert client.post("/api/hosts/local/upgrade", json={}).status_code == 400
    job_id = client.post("/api/hosts/local/upgrade", json={"image": "repo/img:v2"}).json()["job"]
    assert wait_job(client, job_id)["status"] == "ok"
    assert StubDeployer.instances[-1].calls == ["upgrade:repo/img:v2"]


def test_backup_job(client):
    job_id = client.post("/api/hosts/local/backup").json()["job"]
    job = wait_job(client, job_id)
    assert job["result"]["archive"].endswith(".tar.gz")


def test_jobs_are_listed_newest_first(client):
    first = client.post("/api/hosts/local/backup").json()["job"]
    wait_job(client, first)
    second = client.post("/api/hosts/local/backup").json()["job"]
    wait_job(client, second)
    assert [j["id"] for j in client.get("/api/jobs").json()][:2] == [second, first]


def test_an_unknown_job_is_a_404(client):
    assert client.get("/api/jobs/nope").status_code == 404
    assert client.get("/api/jobs/nope/events").status_code == 404


def test_jobs_registry_captures_thread_exceptions():
    """线程里的异常必须落到 job 上，否则界面永远转圈。"""
    jobs = Jobs()

    def boom(say):
        say("开始")
        raise RuntimeError("kaboom")

    job = jobs.start("test", "local", boom)
    for _ in range(200):
        if job.status != "running":
            break
        import time

        time.sleep(0.01)
    assert job.status == "error"
    assert "kaboom" in job.error
    assert job.lines == ["开始"]


def test_extension_push_needs_a_path(client):
    assert client.post("/api/hosts/local/extensions/push", json={}).status_code == 400


def test_unknown_profile_action_is_a_400(client):
    assert client.post("/api/hosts/local/profiles/x/frobnicate").status_code == 400


# --------------------------------------------------------------------------- #
# 界面上的运维与危险动作
# --------------------------------------------------------------------------- #


def test_token_is_retrievable_through_the_ui(client, monkeypatch):
    """Manager 没有用户名密码，token 就是唯一凭据 —— 界面上得能拿到。"""
    monkeypatch.setattr(StubDeployer, "existing_token", lambda self: "t" * 64, raising=False)
    body = client.get("/api/hosts/local/token").json()
    assert body["token"] == "t" * 64
    assert body["env_path"].endswith("/.env")


def test_a_deployment_with_no_token_is_a_404(client, monkeypatch):
    monkeypatch.setattr(StubDeployer, "existing_token", lambda self: None, raising=False)
    assert client.get("/api/hosts/local/token").status_code == 404


def test_rollback_is_a_job(client, monkeypatch):
    monkeypatch.setattr(
        StubDeployer, "rollback",
        lambda self: (self._do("rollback"), DeployResult(self.spec, "t", True, [], {}))[1],
        raising=False,
    )
    job = wait_job(client, client.post("/api/hosts/local/rollback").json()["job"])
    assert job["status"] == "ok"
    assert StubDeployer.instances[-1].calls == ["rollback"]


def test_destroy_keeps_data_by_default(client, monkeypatch):
    seen: list[bool] = []
    monkeypatch.setattr(
        StubDeployer, "destroy",
        lambda self, purge_data=False: seen.append(purge_data), raising=False,
    )
    job = wait_job(client, client.post("/api/hosts/local/destroy", json={}).json()["job"])
    assert job["status"] == "ok"
    assert seen == [False]


def test_purging_data_needs_the_ref_typed_out(client, monkeypatch):
    """一个能被误点的按钮不该能删掉全部登录态。"""
    seen: list[bool] = []
    monkeypatch.setattr(
        StubDeployer, "destroy",
        lambda self, purge_data=False: seen.append(purge_data), raising=False,
    )
    refused = client.post("/api/hosts/local/destroy", json={"purge_data": True})
    assert refused.status_code == 400
    assert "原样打一遍" in refused.json()["detail"]

    # 确认串比对的是解析之后的 host/deployment，不管你怎么寻址它
    wrong = client.post("/api/hosts/local/destroy",
                        json={"purge_data": True, "confirm": "local"})
    assert wrong.status_code == 400
    assert "local/default" in wrong.json()["detail"]
    assert seen == [], "确认没通过就一次都不该执行"

    ok = client.post("/api/hosts/local/destroy",
                     json={"purge_data": True, "confirm": "local/default"})
    assert wait_job(client, ok.json()["job"])["status"] == "ok"
    assert seen == [True]


def test_deleting_a_deployment_leaves_the_target_alone(client):
    client.post("/api/hosts", json={"name": "h", "ssh": "u@x", "deploy": {"dir": "/srv/a"}})
    client.post("/api/deployments",
                json={"host": "h", "name": "second", "spec": {"dir": "/srv/b", "port": 9001}})
    assert client.delete("/api/deployments/h/second").status_code == 200
    assert [d["name"] for d in client.get("/api/deployments?host=h").json()] == ["default"]
    assert StubDeployer.instances == [], "删记录不该碰目标机"
