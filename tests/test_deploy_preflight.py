"""体检。每一项都对应一次真实的失败，所以每一项都得有一个测。"""

from __future__ import annotations

import pytest

from sleight.deploy.preflight import (
    CheckLevel,
    parse_df_avail_kb,
    parse_listening_ports,
    parse_mem_total_kb,
    parse_ps_mounts,
    preflight,
    worst,
)
from sleight.deploy.spec import DeploySpec

from .conftest import FakeRunner, docker_ok

SPEC = DeploySpec(dir="/srv/cbm")


def _run(**kw) -> dict[str, tuple[CheckLevel, str]]:
    spec = kw.pop("spec", SPEC)
    sudo = kw.pop("sudo", False)
    replies = {**docker_ok(), **kw.pop("replies", {})}
    runner = FakeRunner(dirs=kw.pop("dirs", ("/", "/srv")), replies=replies, **kw)
    checks = preflight(spec, runner, sudo=sudo)
    return {c.name: (c.level, f"{c.detail} {c.hint}") for c in checks}


# --------------------------------------------------------------------------- #
# 解析器
# --------------------------------------------------------------------------- #


def test_mem_total():
    assert parse_mem_total_kb("MemTotal:       16316484 kB\nMemFree: 100 kB\n") == 16316484
    assert parse_mem_total_kb("nothing here") is None


def test_df_avail_takes_the_fourth_column():
    output = (
        "Filesystem     1024-blocks      Used Available Capacity Mounted on\n"
        "/dev/nvme0n1p2   494006272 121223168 347619328      26% /\n"
    )
    assert parse_df_avail_kb(output) == 347619328


def test_df_ignores_a_trailing_blank_line():
    assert parse_df_avail_kb("h\n/dev/sda1 100 10 90 10% /\n\n") == 90


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("LISTEN 0 4096 127.0.0.1:9000 0.0.0.0:*", {9000}),
        ("LISTEN 0 4096 *:22 *:*", {22}),
        ("LISTEN 0 4096 [::]:8080 [::]:*", {8080}),
        ("tcp 0 0 127.0.0.1:9000 0.0.0.0:* LISTEN", {9000}),          # netstat 形状
        ("State Recv-Q Send-Q Local-Address:Port Peer-Address:Port", set()),
    ],
)
def test_listening_ports(line, expected):
    assert parse_listening_ports(line) == expected


def test_ps_mounts():
    out = "cbm-01\t/srv/a/data,/etc/localtime\nother\t\nnoisy line without a tab\n"
    assert parse_ps_mounts(out) == {"cbm-01": {"/srv/a/data", "/etc/localtime"}, "other": set()}


def test_worst_level():
    from sleight.deploy.preflight import Check

    ok = Check("a", CheckLevel.OK, "")
    warn = Check("b", CheckLevel.WARN, "")
    bad = Check("c", CheckLevel.FAIL, "")
    assert worst([ok]) is CheckLevel.OK
    assert worst([ok, warn]) is CheckLevel.WARN
    assert worst([ok, warn, bad]) is CheckLevel.FAIL


# --------------------------------------------------------------------------- #
# 体检
# --------------------------------------------------------------------------- #


def test_all_green_on_a_healthy_machine():
    checks = _run()
    assert {name: level for name, (level, _) in checks.items()} == {
        "host": CheckLevel.OK,
        "docker": CheckLevel.OK,
        "compose": CheckLevel.OK,
        "dir": CheckLevel.OK,
        "port": CheckLevel.OK,
        "data": CheckLevel.OK,
        "memory": CheckLevel.OK,
        "disk": CheckLevel.OK,
        "image": CheckLevel.OK,
        "registry": CheckLevel.OK,
        "context": CheckLevel.OK,
    }


def test_unreachable_host_stops_immediately():
    """后面每一项都依赖能执行命令，继续检只会刷一屏没有意义的失败。"""
    runner = FakeRunner(replies={"uname": (255, "")})
    checks = preflight(SPEC, runner)
    assert len(checks) == 1
    assert checks[0].level is CheckLevel.FAIL


def test_docker_socket_permission_gets_the_usermod_hint():
    checks = _run(replies={"docker version": (1, "permission denied while trying to connect")})
    level, text = checks["docker"]
    assert level is CheckLevel.FAIL
    assert "usermod -aG docker" in text
    assert "compose" not in checks              # 没 docker 就没必要继续


def test_daemon_down_gets_the_systemctl_hint():
    checks = _run(replies={"docker version": (1, "Cannot connect to the Docker daemon")})
    assert "systemctl" in checks["docker"][1]


def test_compose_v1_is_named_as_the_problem():
    """装了 docker-compose v1 的机器上，"装一下插件"是错的建议。"""
    checks = _run(replies={"docker compose version": (1, ""), "docker-compose --version": (0, "1.29.2")})
    level, text = checks["compose"]
    assert level is CheckLevel.FAIL
    assert "end-of-life" in text


def test_unwritable_dir_fails_but_sudo_makes_it_ok():
    assert _run(unwritable=["/srv"])["dir"][0] is CheckLevel.FAIL
    assert "--sudo" in _run(unwritable=["/srv"])["dir"][1]
    assert _run(unwritable=["/srv"], sudo=True)["dir"][0] is CheckLevel.OK


def test_existing_writable_dir_is_ok():
    assert _run(dirs=("/", "/srv", "/srv/cbm"))["dir"][0] is CheckLevel.OK


def test_port_taken_by_someone_else_fails():
    checks = _run(replies={"ss -ltn": (0, "LISTEN 0 4096 127.0.0.1:9000 0.0.0.0:*")})
    level, text = checks["port"]
    assert level is CheckLevel.FAIL
    assert "9000" in text


def test_port_held_by_our_own_container_is_a_redeploy_not_a_conflict():
    checks = _run(replies={
        "ss -ltn": (0, "LISTEN 0 4096 127.0.0.1:9000 0.0.0.0:*"),
        "docker ps --filter": (0, "cloakbrowser-manager"),
    })
    level, text = checks["port"]
    assert level is CheckLevel.OK
    assert "redeploy" in text


def test_no_ss_or_netstat_degrades_to_a_warning():
    """探测工具缺失不该阻塞部署 —— docker 自己会在冲突时报错。"""
    checks = _run(replies={"ss -ltn": (127, ""), "netstat -ltn": (127, "")})
    assert checks["port"][0] is CheckLevel.WARN


def test_two_managers_on_one_data_dir_is_refused():
    """手册 A.3 的第四条禁令。后果是两个 Manager 互相踩 profile 数据库。"""
    checks = _run(replies={"docker ps -a --format": (0, "other-manager\t/srv/cbm/data,/etc/hosts")})
    level, text = checks["data"]
    assert level is CheckLevel.FAIL
    assert "other-manager" in text
    assert "own --dir" in text


def test_our_own_container_mounting_it_is_fine():
    checks = _run(replies={
        "docker ps -a --format": (0, "cloakbrowser-manager\t/srv/cbm/data")
    })
    assert checks["data"][0] is CheckLevel.OK


def test_low_memory_warns_with_the_baseline_numbers():
    checks = _run(replies={"cat /proc/meminfo": (0, "MemTotal: 4000000 kB")})
    level, text = checks["memory"]
    assert level is CheckLevel.WARN
    assert "8 GB" in text and "16 GB" in text


def test_eight_gb_is_ok_but_still_mentions_sixteen():
    checks = _run(replies={"cat /proc/meminfo": (0, f"MemTotal: {9 * 1024 * 1024} kB")})
    assert checks["memory"][0] is CheckLevel.OK
    assert "16 GB" in checks["memory"][1]


def test_low_disk_warns():
    checks = _run(replies={"df -Pk": (0, "h\n/dev/sda1 100 10 5000000 90% /")})
    assert checks["disk"][0] is CheckLevel.WARN


def test_image_check_says_tag_vs_digest_and_cached_or_not():
    assert "tag 'v0.0.10'" in _run()["image"][1]
    assert "will be pulled" in _run()["image"][1]
    assert "already pulled" in _run(replies={"docker image inspect": (0, "sha256:x")})["image"][1]
    digest = DeploySpec(dir="/srv/cbm", image="repo/img:v1@sha256:" + "a" * 64)
    assert "digest-pinned" in _run(spec=digest)["image"][1]


# --------------------------------------------------------------------------- #
# registry 可达性
# --------------------------------------------------------------------------- #

PROXY_SHELL = 'sh -c printf %s "${HTTPS_PROXY:-${https_proxy:-${HTTP_PROXY:-$http_proxy}}}"'


def test_a_shell_proxy_the_daemon_does_not_have_is_flagged():
    """docker pull 是 systemd 起的守护进程干的，它读不到你 shell 的 HTTP_PROXY。

    症状是 pull 卡满超时后一句 context deadline exceeded —— 极容易被当成网络抖动。
    """
    checks = _run(replies={
        "docker info": (0, "|"),
        PROXY_SHELL: (0, "http://192.168.1.1:7890"),
    })
    level, text = checks["registry"]
    assert level is CheckLevel.WARN
    assert text.count("192.168.1.1:7890") == 1, "两个代理变量不该被拼成一串"
    assert "context deadline exceeded" in text
    assert "systemd" in text


def test_a_daemon_with_its_own_proxy_is_fine():
    checks = _run(replies={
        "docker info": (0, "http://proxy:3128|http://proxy:3128"),
        PROXY_SHELL: (0, "http://proxy:3128"),
    })
    level, text = checks["registry"]
    assert level is CheckLevel.OK
    # HTTP 和 HTTPS 通常是同一个地址，报两遍只是噪音
    assert text.count("http://proxy:3128") == 1


def test_different_http_and_https_proxies_are_both_shown():
    checks = _run(replies={
        "docker info": (0, "http://a:3128|http://b:3128"),
        PROXY_SHELL: (0, "http://a:3128"),
    })
    assert "http://a:3128, http://b:3128" in checks["registry"][1]


def test_no_proxy_anywhere_is_fine():
    checks = _run(replies={"docker info": (0, "|"), PROXY_SHELL: (0, "")})
    assert checks["registry"][0] is CheckLevel.OK


def test_registry_is_not_checked_when_the_image_is_already_local():
    """镜像在本地就不会去 registry，代理配没配都无所谓。"""
    checks = _run(replies={
        "docker image inspect": (0, "sha256:x"),
        "docker info": (0, "|"),
        PROXY_SHELL: (0, "http://192.168.1.1:7890"),
    })
    assert "registry" not in checks


# --------------------------------------------------------------------------- #
# docker context
# --------------------------------------------------------------------------- #


def test_the_default_context_is_quiet():
    checks = _run(replies={"docker context show": (0, "default")})
    assert checks["context"][0] is CheckLevel.OK


def test_a_remote_context_is_flagged():
    """docker 跟随 context —— 设过远程 context 的话，"本机部署"会悄悄发到别的机器上，
    而体检的其余每一项量的都是 runner 所在的这台。"""
    checks = _run(replies={
        "docker context show": (0, "hostB"),
        "docker context inspect": (0, "ssh://kaliB"),
    })
    level, text = checks["context"]
    assert level is CheckLevel.WARN
    assert "hostB" in text and "ssh://kaliB" in text
    assert "docker context use default" in text


def test_an_old_docker_without_context_support_is_not_a_problem():
    checks = _run(replies={"docker context show": (127, "")})
    assert checks["context"][0] is CheckLevel.OK
