"""Deployer 的全生命周期。

断言的是**发出去的命令序列**和**落到目标机上的文件**，因为这两样就是这一层的全部
可观察行为。护栏（永不 ``down -v``、不换存量 token、不递归 chown data/）各有一个测 ——
它们都是"一次就很贵"的那类错误。
"""

from __future__ import annotations

import json

import pytest

from sleight.deploy.engine import Deployer
from sleight.deploy.errors import DeployError, PreflightFailed
from sleight.deploy.render import parse_env
from sleight.deploy.spec import DeploySpec

from .conftest import FakeRunner, docker_ok

SPEC = DeploySpec(dir="/srv/cbm")
STATUS = '{"running_count": 0, "binary_version": "146.0.7680.177.5", "profiles_total": 3}'
RUNNING = (0, "running\thealthy\tcloakhq/cloakbrowser-manager:v0.0.10")


def make(
    *, files=None, dirs=("/", "/srv"), replies=None, spec=SPEC, unwritable=(), **kw
) -> tuple[Deployer, FakeRunner]:
    merged = {**docker_ok(), "docker exec": (0, STATUS), **(replies or {})}
    runner = FakeRunner(files=files, dirs=dirs, replies=merged, unwritable=unwritable)
    return Deployer(spec, runner, **kw), runner


def deployed(**kw) -> tuple[Deployer, FakeRunner]:
    """一台已经部署好、容器在跑的机器。"""
    dep, runner = make(replies={"docker inspect --format": RUNNING}, **kw)
    dep.apply()
    runner.commands.clear()
    return dep, runner


# --------------------------------------------------------------------------- #
# plan
# --------------------------------------------------------------------------- #


def test_plan_on_a_fresh_machine_lists_everything():
    dep, _ = make()
    plan = dep.plan()
    joined = " ".join(plan.changes)
    assert "创建目录" in joined
    assert "docker-compose.yaml" in joined
    assert "AUTH_TOKEN" in joined
    assert "拉镜像并创建容器" in joined
    assert plan.commands[0] == ("docker", "compose", "pull", "manager")
    assert plan.commands[1] == ("docker", "compose", "up", "-d")
    assert not plan.blocked


def test_plan_touches_nothing():
    """--dry-run 的全部价值就在这一条。"""
    dep, runner = make()
    dep.plan()
    assert runner.files == {}
    assert not runner.ran("docker", "compose", "pull")
    assert not runner.ran("docker", "compose", "up")


def test_plan_renders_the_exact_files_that_will_be_written():
    dep, _ = make()
    plan = dep.plan()
    assert set(plan.files) == {"/srv/cbm/docker-compose.yaml", "/srv/cbm/.env"}
    assert "container_name: cloakbrowser-manager" in plan.files["/srv/cbm/docker-compose.yaml"]
    assert plan.render().count("/srv/cbm") > 1


def test_plan_is_blocked_by_a_failing_check():
    dep, _ = make(replies={"ss -ltn": (0, "LISTEN 0 4096 127.0.0.1:9000 0.0.0.0:*")})
    assert dep.plan().blocked


def test_plan_on_an_up_to_date_machine_has_no_changes():
    dep, _ = deployed()
    plan = dep.plan()
    assert plan.up_to_date
    assert plan.commands == []


# --------------------------------------------------------------------------- #
# apply
# --------------------------------------------------------------------------- #


def test_apply_writes_files_with_the_right_modes():
    dep, runner = make()
    dep.apply()
    assert "name: cloakbrowser" in runner.files["/srv/cbm/docker-compose.yaml"]
    assert runner.modes["/srv/cbm/.env"] == 0o600      # 里面有 token
    assert runner.modes["/srv/cbm/docker-compose.yaml"] == 0o644


def test_apply_creates_data_and_backups():
    dep, runner = make()
    dep.apply()
    assert "/srv/cbm/data" in runner.dirs
    assert "/srv/cbm/backups" in runner.dirs


def test_apply_order_is_pull_then_up():
    dep, runner = make()
    dep.apply()
    order = [c for c in runner.commands if c[:2] == ("docker", "compose") and c[2] != "version"]
    assert order[0] == ("docker", "compose", "pull", "manager")
    assert order[1] == ("docker", "compose", "up", "-d")


def test_compose_runs_in_the_deploy_dir_so_env_is_picked_up():
    """``.env`` 是靠工作目录被自动读取的，cwd 错了 token 就是空。"""
    dep, runner = make()
    calls: list[str | None] = []
    original = runner.run

    def spy(argv, **kw):
        argv = tuple(argv)
        if argv[:2] == ("docker", "compose") and argv[2] != "version":
            calls.append(kw.get("cwd"))
        return original(argv, **kw)

    runner.run = spy
    dep.apply()
    assert calls and set(calls) == {"/srv/cbm"}


def test_apply_returns_the_api_status():
    dep, _ = make()
    result = dep.apply()
    assert result.changed
    assert result.status["profiles_total"] == 3
    assert "146.0.7680.177.5" in result.summary


def test_a_generated_token_is_64_hex_chars():
    dep, runner = make()
    token = dep.apply().token
    assert len(token) == 64
    assert parse_env(runner.files["/srv/cbm/.env"])["AUTH_TOKEN"] == token


def test_existing_token_is_never_silently_replaced():
    """换掉等于所有在用的客户端同时 401，而旧值散落在别的机器的环境变量里。"""
    dep, runner = deployed(files={"/srv/cbm/.env": "AUTH_TOKEN=keepme\n"})
    assert dep.existing_token() == "keepme"
    dep.apply()
    assert parse_env(runner.files["/srv/cbm/.env"])["AUTH_TOKEN"] == "keepme"


def test_hand_added_env_keys_survive_a_redeploy():
    dep, runner = deployed(files={"/srv/cbm/.env": "AUTH_TOKEN=t\nMY_FLAG=1\n"})
    dep.apply()
    assert parse_env(runner.files["/srv/cbm/.env"])["MY_FLAG"] == "1"


def test_second_apply_changes_nothing():
    """幂等：配置没变、容器在跑，就连 up -d 都不发。"""
    dep, runner = deployed()
    result = dep.apply()
    assert not result.changed
    assert not runner.ran("docker", "compose", "up", "-d")
    assert not runner.ran("docker", "compose", "pull", "manager")


def test_a_stopped_container_gets_started_even_with_no_config_change():
    dep, runner = deployed()
    runner.replies["docker inspect --format"] = (0, "exited\tnone\timg")
    runner.replies["docker exec"] = (0, STATUS)
    dep.apply(wait=False)
    assert runner.ran("docker", "compose", "up", "-d")


def test_changing_the_port_rewrites_env_and_recreates():
    dep, runner = deployed()
    dep.spec = dep.spec.replace(port=9100)
    result = dep.apply(wait=False)
    assert result.changed
    assert parse_env(runner.files["/srv/cbm/.env"])["MANAGER_PORT"] == "9100"
    assert runner.ran("docker", "compose", "up", "-d")


def test_force_recreate_uses_no_deps():
    dep, runner = deployed()
    dep.apply(force_recreate=True, wait=False)
    assert runner.ran("docker", "compose", "up", "-d", "--no-deps", "--force-recreate", "manager")


def test_apply_refuses_on_a_failed_check_but_force_overrides():
    dep, runner = make(replies={"ss -ltn": (0, "LISTEN 0 4096 127.0.0.1:9000 0.0.0.0:*")})
    with pytest.raises(PreflightFailed, match="port 9000"):
        dep.apply()
    assert runner.files == {}                       # 失败时什么都没写
    dep.apply(force=True, wait=False)
    assert runner.files


def test_dry_run_records_without_touching_anything():
    dep, runner = make(dry_run=True)
    dep.apply()
    assert runner.files == {}
    assert ("docker", "compose", "up", "-d") in dep.recorded
    assert not runner.ran("docker", "compose", "up", "-d")


def test_state_file_records_the_image_but_never_the_token():
    dep, runner = make()
    token = dep.apply().token
    state = json.loads(runner.files["/srv/cbm/.sleight-deploy.json"])
    assert state["image"] == SPEC.image
    assert state["managed_by"] == "sleight"
    assert token not in runner.files["/srv/cbm/.sleight-deploy.json"]


def test_progress_callback_gets_every_step():
    lines: list[str] = []
    dep, _ = make(on_progress=lines.append)
    dep.apply()
    assert any("写入" in line for line in lines)
    assert any("healthy" in line for line in lines)


# --------------------------------------------------------------------------- #
# sudo
# --------------------------------------------------------------------------- #


def test_sudo_hands_the_new_dir_back_to_the_user():
    """用 sudo 建出来的目录归 root，之后写 compose 还得继续 sudo。"""
    dep, runner = make(sudo=True, unwritable=["/srv"])
    dep.apply(wait=False)
    chowns = [c for c in runner.commands if c[0] == "chown"]
    assert [c[-1] for c in chowns] == ["/srv/cbm", "/srv/cbm/data", "/srv/cbm/backups"]
    assert not any("-R" in c for c in chowns), "递归 chown 会动 data/ 里的浏览器用户数据"


def test_docker_itself_is_never_run_under_sudo():
    """当前用户应该在 docker 组里。给 docker 加 sudo 会掩盖权限配置问题。"""
    dep, runner = make(sudo=True)
    dep.apply(wait=False)
    assert not any(c[0] == "docker" for c in runner.sudo_commands)


def test_no_sudo_by_default():
    dep, runner = make()
    dep.apply(wait=False)
    assert runner.sudo_commands == []


# --------------------------------------------------------------------------- #
# 等待就绪
# --------------------------------------------------------------------------- #


def test_a_container_that_died_reports_its_logs():
    dep, _runner = make(replies={
        "docker inspect --format": (0, "exited\tnone\timg"),
        "docker logs": (1, "shm_size too small"),
    })
    with pytest.raises(DeployError, match="shm_size too small"):
        dep.apply()


def test_no_healthcheck_falls_back_to_api_status():
    """用户可能自己改过 compose 去掉 healthcheck，那时 /api/status 就是唯一判据。"""
    dep, _ = make(replies={"docker inspect --format": (0, "running\tnone\timg")})
    assert dep.apply().status["profiles_total"] == 3


def test_health_timeout_points_at_the_logs():
    dep, _ = make(replies={"docker inspect --format": (0, "running\tstarting\timg")})
    with pytest.raises(DeployError, match="docker compose logs"):
        dep.wait_healthy(timeout=0.1)


def test_api_status_is_probed_inside_the_container():
    """宿主机不一定有 curl 或 python；镜像自己的 healthcheck 用的就是容器内的 python。"""
    dep, runner = deployed()
    dep.api_status()
    exec_cmd = runner.find("docker", "exec")[0]
    assert exec_cmd[2] == "cloakbrowser-manager"
    assert exec_cmd[3] == "python"
    assert "127.0.0.1:8080/api/status" in exec_cmd[-1]


def test_unparseable_api_status_is_empty_not_a_crash():
    dep, _ = deployed()
    dep.runner.replies["docker exec"] = (0, "<html>502</html>")
    assert dep.api_status() == {}


# --------------------------------------------------------------------------- #
# 备份
# --------------------------------------------------------------------------- #


def test_backup_stops_archives_then_starts():
    """在线复制 SQLite 会拿到和浏览器用户目录不一致的时间点（手册 A.8）。"""
    dep, runner = deployed()
    archive = dep.backup()
    steps = [
        c for c in runner.commands
        if c[:2] in (("docker", "compose"), ("docker", "run"))
    ]
    assert steps[0] == ("docker", "compose", "stop", "manager")
    assert steps[1][:2] == ("docker", "run")
    assert steps[2] == ("docker", "compose", "start", "manager")
    assert archive.startswith("/srv/cbm/backups/cloakbrowser-")
    assert archive.endswith(".tar.gz")


def test_backup_runs_as_root_in_a_throwaway_container():
    """data/ 里是 Manager 以 root 写的文件 —— 宿主机上的普通用户 tar 会一路
    Permission denied，而且会留下一个只有十几条的残档。"""
    dep, runner = deployed()
    dep.backup()
    run = runner.find("docker", "run")[0]
    assert "--rm" in run
    assert "-v" in run and f"{SPEC.data_dir}:/data:ro" in run
    assert f"{SPEC.backups_dir}:/backup" in run
    assert SPEC.image in run
    script = run[-1]
    assert "tar -czf /backup/cloakbrowser-" in script
    assert "-C / data" in script


def test_a_failed_backup_leaves_nothing_that_looks_like_a_backup():
    """一个看着像备份的残档比没有备份危险得多。"""
    dep, runner = deployed()
    dep.backup()
    script = runner.find("docker", "run")[0][-1]
    # 先写 .part，成功才改名；失败就把 .part 删掉并以非 0 退出
    assert ".part" in script
    assert "rm -f /backup/cloakbrowser-" in script
    assert "exit 1" in script
    assert script.index("mv /backup/") > script.index("tar -czf")


def test_backup_hands_the_archive_back_to_the_host_user():
    """容器里是 root，产出的文件默认归 root —— 那样用户连删旧备份都要 sudo。"""
    dep, runner = deployed()
    dep.backup()
    assert "chown 1000:1000 /backup/" in runner.find("docker", "run")[0][-1]


def test_an_unknown_host_uid_skips_the_chown_instead_of_using_root():
    """宁可留个 root 拥有的档，也不能 chown 到 0:0 —— 那是把问题固化。"""
    dep, runner = deployed()
    runner.replies["id -u"] = (1, "")
    dep.backup()
    assert "chown" not in runner.find("docker", "run")[0][-1]


def test_backup_restarts_even_if_the_archive_fails():
    dep, runner = deployed()
    runner.replies["docker run"] = (1, "tar: no space left on device")
    with pytest.raises(DeployError, match="no space left"):
        dep.backup()
    assert runner.ran("docker", "compose", "start", "manager")


def test_backup_of_a_stopped_manager_does_not_start_it():
    dep, runner = deployed()
    runner.replies["docker inspect --format"] = (0, "exited\tnone\timg")
    dep.backup()
    assert not runner.ran("docker", "compose", "stop", "manager")
    assert not runner.ran("docker", "compose", "start", "manager")


# --------------------------------------------------------------------------- #
# 升级与回滚
# --------------------------------------------------------------------------- #


def test_upgrade_needs_something_to_upgrade():
    dep, _ = make()
    with pytest.raises(DeployError, match="run a deploy first"):
        dep.upgrade("repo/img:v2")


def test_upgrade_rewrites_the_image_and_remembers_the_old_one():
    dep, runner = deployed()
    dep.upgrade("cloakhq/cloakbrowser-manager:v0.0.11", backup=False, wait=False)
    env = parse_env(runner.files["/srv/cbm/.env"])
    assert env["MANAGER_IMAGE"] == "cloakhq/cloakbrowser-manager:v0.0.11"
    state = json.loads(runner.files["/srv/cbm/.sleight-deploy.json"])
    assert state["previous_image"] == "cloakhq/cloakbrowser-manager:v0.0.10"
    assert runner.ran("docker", "compose", "up", "-d", "--no-deps", "--force-recreate", "manager")


def test_upgrade_backs_up_first_by_default():
    dep, runner = deployed()
    dep.upgrade("repo/img:v2", wait=False)
    assert runner.ran("docker", "run")


def test_rollback_uses_the_recorded_previous_image():
    dep, runner = deployed()
    dep.upgrade("repo/img:v2", backup=False, wait=False)
    dep.rollback(wait=False)
    assert parse_env(runner.files["/srv/cbm/.env"])["MANAGER_IMAGE"] == (
        "cloakhq/cloakbrowser-manager:v0.0.10"
    )


def test_rollback_without_history_says_so():
    dep, _ = deployed()
    with pytest.raises(DeployError, match="previous_image"):
        dep.rollback()


def test_a_corrupt_state_file_does_not_break_status():
    dep, _ = make(files={"/srv/cbm/.sleight-deploy.json": "{not json"})
    assert dep.read_state() == {}
    assert dep.status()["state_file"] == {}


# --------------------------------------------------------------------------- #
# 销毁
# --------------------------------------------------------------------------- #


def test_destroy_never_removes_volumes():
    """``down -v`` 会连卷一起删。手册里写着不要执行它。"""
    dep, runner = deployed()
    dep.destroy()
    assert runner.ran("docker", "compose", "down")
    assert not any("-v" in c or "--volumes" in c for c in runner.commands)


def test_destroy_keeps_data_by_default():
    dep, runner = deployed()
    dep.destroy()
    assert "/srv/cbm/data" in runner.dirs


def test_purge_data_is_the_only_way_to_lose_logins():
    dep, runner = deployed()
    dep.destroy(purge_data=True)
    assert runner.ran("rm", "-rf", "/srv/cbm/data")


def test_purge_falls_back_to_a_root_container_when_rm_is_denied():
    """浏览器用户目录是容器以 root 写的，普通用户 rm 删不掉。"""
    dep, runner = deployed()
    runner.replies["rm -rf"] = (1, "rm: cannot remove '.../Local State': Permission denied")
    dep.destroy(purge_data=True)
    run = runner.find("docker", "run")[0]
    assert f"{SPEC.dir}:/target" in run
    assert "rm -rf /target/data" in run[-1]


def test_purge_that_cannot_delete_at_all_says_what_to_run_by_hand():
    dep, runner = deployed()
    runner.replies["rm -rf"] = (1, "Permission denied")
    runner.replies["docker run"] = (1, "docker: no such image")
    with pytest.raises(DeployError, match="sudo rm -rf /srv/cbm/data"):
        dep.destroy(purge_data=True)


def test_destroy_without_a_compose_file_still_removes_the_container():
    dep, runner = make()
    dep.destroy()
    assert runner.ran("docker", "rm", "-f", "cloakbrowser-manager")


# --------------------------------------------------------------------------- #
# 状态与连接
# --------------------------------------------------------------------------- #


def test_status_shape():
    dep, _ = deployed()
    dep.runner.replies["docker port"] = (0, "8080/tcp -> 127.0.0.1:9000")
    status = dep.status()
    assert status["container"]["status"] == "running"
    assert status["api"]["profiles_total"] == 3
    assert status["ports"] == ["8080/tcp -> 127.0.0.1:9000"]
    assert status["url"] == "http://127.0.0.1:9000"


def test_status_of_an_absent_container():
    dep, _ = make()
    status = dep.status()
    assert status["container"] == {
        "exists": "no", "status": "absent", "health": "none", "image": ""
    }
    assert status["api"] == {} and status["ports"] == []


def test_connect_without_a_token_says_where_it_looked():
    dep, _ = make()
    with pytest.raises(DeployError, match=r"/srv/cbm/\.env"), dep.connect():
        pass


def test_connect_builds_a_manager_client_through_the_tunnel():
    dep, _ = deployed(files={"/srv/cbm/.env": "AUTH_TOKEN=tok\n"})
    with dep.connect() as mgr:
        assert mgr.token == "tok"
        assert mgr.base_url == "http://127.0.0.1:9000"


def test_logs_does_not_count_as_a_mutation():
    dep, runner = deployed()
    dep.logs(tail=50)
    assert runner.ran("docker", "compose", "logs", "--tail", "50", "manager")
    assert ("docker", "compose", "logs", "--tail", "50", "manager") not in dep.recorded


# --------------------------------------------------------------------------- #
# 噪音控制
# --------------------------------------------------------------------------- #


def test_warnings_are_said_on_a_real_deploy():
    lines: list[str] = []
    dep, _ = make(on_progress=lines.append)
    dep.apply()
    assert any("digest" in line for line in lines)


def test_a_no_op_redeploy_stays_quiet():
    """每次都响的提醒会训练人忽略提醒 —— 什么都没变时就别念了。"""
    dep, _ = deployed()
    lines: list[str] = []
    dep._on_progress = lines.append
    dep.apply()
    assert not any("提醒" in line for line in lines)
    assert any("已经是这个状态" in line for line in lines)


# --------------------------------------------------------------------------- #
# 拉镜像：尽力而为，不是必须成功
# --------------------------------------------------------------------------- #


def test_a_failed_pull_is_survivable_when_the_image_is_already_there():
    """内网/离线的目标机连不上 registry 很常见，而镜像可能是 docker load 进去的。

    真机上就是这么炸的：镜像明明在目标机上，pull 失败却把整个部署挡死了。
    """
    dep, runner = make(replies={
        "docker image inspect": (0, "sha256:cafe"),
        "docker compose pull": (1, 'Get "https://registry-1.docker.io/v2/": timeout'),
    })
    lines: list[str] = []
    dep._on_progress = lines.append
    result = dep.apply()
    assert result.changed
    assert runner.ran("docker", "compose", "up", "-d")
    assert any("就用本地那份" in line for line in lines)


def test_a_failed_pull_without_a_local_image_stops_and_says_how_to_get_it_there():
    dep, runner = make(replies={
        "docker image inspect": (1, ""),
        "docker compose pull": (1, 'Get "https://registry-1.docker.io/v2/": timeout'),
    })
    with pytest.raises(DeployError, match="docker save") as exc:
        dep.apply()
    assert "registry-1.docker.io" in str(exc.value)
    assert not runner.ran("docker", "compose", "up", "-d")


def test_a_successful_pull_says_nothing_extra():
    dep, _ = make()
    lines: list[str] = []
    dep._on_progress = lines.append
    dep.apply()
    assert not any("就用本地那份" in line for line in lines)
