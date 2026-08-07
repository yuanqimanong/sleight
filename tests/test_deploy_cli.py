"""主机清单与命令行。

CLI 的测法：把 ``_resolve`` 换成一个 :class:`FakeRunner`，然后断言**退出码**和**输出**。
退出码是脚本唯一能依赖的契约（0 成功 / 1 失败 / 2 用法 / 3 体检没过），所以每一种都测。
"""

from __future__ import annotations

import json

import pytest

from sleight import cli
from sleight.deploy.inventory import sleight_home
from sleight.deploy.render import parse_env
from sleight.deploy.spec import DeploySpec
from sleight.deploy.store import Store

from .conftest import FakeRunner, docker_ok

SPEC = DeploySpec(dir="/srv/cbm")
STATUS = '{"running_count": 1, "binary_version": "146.0.7680.177.5", "profiles_total": 3}'
RUNNING = (0, "running\thealthy\tcloakhq/cloakbrowser-manager:v0.0.10")


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """别去碰真的 ~/.sleight。"""
    monkeypatch.setenv("SLEIGHT_HOME", str(tmp_path / "home"))
    return tmp_path / "home"


@pytest.fixture
def target(monkeypatch):
    """把 CLI 的目标机换成替身，返回那个 FakeRunner。"""
    runner = FakeRunner(
        dirs=("/", "/srv"),
        replies={**docker_ok(), "docker exec": (0, STATUS)},
    )

    def resolve(args):
        """照着真的 _resolve 走一遍，只是把 Runner 换成替身。

        给了 --host 就真去库里查 —— 记流水那条路径依赖的就是这个记录。
        """
        deployment = Store().resolve(args.host) if getattr(args, "host", None) else None
        spec = deployment.spec if deployment else SPEC
        for flag, field in cli._SPEC_FLAGS.items():
            value = getattr(args, flag, None)
            if value is not None:
                spec = spec.replace(**{field: value})
        return runner, spec, bool(getattr(args, "sudo", False)), deployment

    monkeypatch.setattr(cli, "_resolve", resolve)
    return runner


# --------------------------------------------------------------------------- #
# 本地库
# --------------------------------------------------------------------------- #


def test_home_follows_the_env_var(isolated_home):
    assert sleight_home() == isolated_home


def test_the_cli_and_the_store_agree_on_where_the_db_is():
    """CLI 每条命令都新开一个 Store，路径必须稳定，不然写进去的下一条命令读不到。"""
    assert Store().path == sleight_home() / "sleight.db"


# --------------------------------------------------------------------------- #
# 命令行骨架
# --------------------------------------------------------------------------- #


def test_version_exits_zero(capsys):
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["--version"])
    assert exit_info.value.code == 0
    assert "sleight" in capsys.readouterr().out


def test_no_command_prints_help_and_returns_usage(capsys):
    assert cli.main([]) == cli.EXIT_USAGE
    assert "deploy" in capsys.readouterr().out


def test_bad_command_is_argparse_usage_error():
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["nonsense"])
    assert exit_info.value.code == 2


def test_host_and_ssh_together_is_refused(capsys):
    assert cli.main(["status", "--host", "a", "--ssh", "u@h"]) == cli.EXIT_ERROR
    assert "mutually exclusive" in capsys.readouterr().err


def test_a_library_error_becomes_exit_one_not_a_traceback(capsys, target):
    assert cli.main(["token"]) == cli.EXIT_ERROR
    assert "AUTH_TOKEN" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# deploy
# --------------------------------------------------------------------------- #


def test_dry_run_prints_the_plan_and_touches_nothing(capsys, target):
    assert cli.main(["deploy", "--dry-run"]) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "体检" in out and "变更" in out
    assert "docker-compose.yaml" in out
    assert "AUTH_TOKEN" in out                       # 文件全文也打出来
    assert target.files == {}


def test_dry_run_brief_omits_the_file_bodies(capsys, target):
    cli.main(["deploy", "--dry-run", "--brief"])
    out = capsys.readouterr().out
    assert "将执行" in out
    assert "healthcheck" not in out


def test_dry_run_returns_three_when_a_check_fails(target):
    target.replies["ss -ltn"] = (0, "LISTEN 0 4096 127.0.0.1:9000 0.0.0.0:*")
    assert cli.main(["deploy", "--dry-run"]) == cli.EXIT_PREFLIGHT


def test_deploy_prints_the_token_and_how_to_connect(capsys, target):
    assert cli.main(["deploy"]) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "AUTH_TOKEN" in out
    assert "CloakBrowserManager" in out
    assert "http://127.0.0.1:9000" in out


def test_a_redeploy_only_shows_a_token_prefix(capsys, target):
    """第一次部署要给出完整 token（人得拿走），之后就只给前缀。"""
    cli.main(["deploy"])
    token = parse_env(target.files["/srv/cbm/.env"])["AUTH_TOKEN"]
    capsys.readouterr()
    cli.main(["deploy"])
    out = capsys.readouterr().out
    assert token not in out
    assert "sleight token" in out


def test_deploy_json_is_machine_readable(capsys, target):
    assert cli.main(["--json", "deploy"]) == cli.EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["changed"] is True
    assert len(payload["token"]) == 64
    assert payload["status"]["profiles_total"] == 3


def test_spec_flags_reach_the_spec(capsys, target):
    cli.main(["deploy", "--dry-run", "--port", "9100", "--image", "repo/img:v2"])
    out = capsys.readouterr().out
    assert "MANAGER_PORT=9100" in out
    assert "repo/img:v2" in out


def test_latest_is_refused_at_the_cli_too(capsys, target):
    assert cli.main(["deploy", "--image", "repo/img:latest"]) == cli.EXIT_ERROR
    assert "not reproducible" in capsys.readouterr().err
    assert cli.main(["deploy", "--dry-run", "--image", "repo/img:latest", "--allow-latest"]) == 0


def test_expose_is_needed_for_a_public_bind(capsys, target):
    assert cli.main(["deploy", "--bind", "0.0.0.0"]) == cli.EXIT_ERROR
    assert "cleartext" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# 其它命令
# --------------------------------------------------------------------------- #


def test_preflight_exit_codes(target):
    assert cli.main(["preflight"]) == cli.EXIT_OK
    target.replies["docker version"] = (1, "permission denied")
    assert cli.main(["preflight"]) == cli.EXIT_PREFLIGHT


def test_preflight_json_lists_every_check(capsys, target):
    cli.main(["--json", "preflight"])
    checks = json.loads(capsys.readouterr().out)
    assert {c["name"] for c in checks} >= {"docker", "compose", "port", "data", "memory"}


def test_status_returns_one_when_nothing_is_running(capsys, target):
    assert cli.main(["status"]) == cli.EXIT_ERROR
    assert "absent" in capsys.readouterr().out


def test_status_of_a_running_manager(capsys, target):
    cli.main(["deploy"])
    capsys.readouterr()
    assert cli.main(["status"]) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "146.0.7680.177.5" in out
    assert "profile 3 个" in out


def test_logs_follow_streams_instead_of_capturing(target):
    cli.main(["deploy"])
    assert cli.main(["logs", "-f", "--tail", "10"]) == cli.EXIT_OK
    assert target.streamed[-1] == ("docker", "compose", "logs", "-f", "--tail", "10", "manager")


def test_token_prints_just_the_token(capsys, target):
    cli.main(["deploy"])
    capsys.readouterr()
    assert cli.main(["token"]) == cli.EXIT_OK
    assert len(capsys.readouterr().out.strip()) == 64


def test_backup_names_the_archive(capsys, target):
    cli.main(["deploy"])
    capsys.readouterr()
    assert cli.main(["backup"]) == cli.EXIT_OK
    assert "/srv/cbm/backups/cloakbrowser-" in capsys.readouterr().out


def test_destroy_keeps_data_by_default(capsys, target):
    cli.main(["deploy"])
    capsys.readouterr()
    assert cli.main(["destroy"]) == cli.EXIT_OK
    assert "data/ 保留" in capsys.readouterr().out


def test_purge_data_refuses_without_confirmation(capsys, target):
    """非交互环境下删 /data 必须显式 --yes，否则一律拒绝。"""
    cli.main(["deploy"])
    capsys.readouterr()
    assert cli.main(["destroy", "--purge-data"]) == cli.EXIT_ERROR
    assert not target.ran("rm", "-rf", "/srv/cbm/data")
    assert "--yes" in capsys.readouterr().err


def test_purge_data_with_yes_goes_through(target):
    cli.main(["deploy"])
    assert cli.main(["destroy", "--purge-data", "--yes"]) == cli.EXIT_OK
    assert target.ran("rm", "-rf", "/srv/cbm/data")


def test_purge_image_needs_no_confirmation(capsys, target):
    """删镜像可逆（重新 pull 就回来），不该跟删 data/ 一样要 --yes。"""
    cli.main(["deploy"])
    capsys.readouterr()
    assert cli.main(["destroy", "--purge-image"]) == cli.EXIT_OK
    assert target.ran("docker", "image", "rm")
    out = capsys.readouterr().out
    assert "镜像" in out and "data/ 保留" in out


def test_purging_everything_in_one_go(capsys, target):
    cli.main(["deploy"])
    capsys.readouterr()
    assert cli.main(["destroy", "--purge-data", "--purge-image", "--yes"]) == cli.EXIT_OK
    assert target.ran("rm", "-rf", "/srv/cbm/data")
    assert target.ran("docker", "image", "rm")


def test_upgrade_swaps_the_image(capsys, target):
    cli.main(["deploy"])
    capsys.readouterr()
    assert cli.main(["upgrade", "cloakhq/cloakbrowser-manager:v0.0.11", "--no-backup"]) == 0
    assert "MANAGER_IMAGE=cloakhq/cloakbrowser-manager:v0.0.11" in target.files["/srv/cbm/.env"]


def test_tunnel_on_a_local_target_says_it_is_unnecessary(capsys, target):
    assert cli.main(["tunnel"]) == cli.EXIT_OK
    assert "不需要隧道" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# hosts
# --------------------------------------------------------------------------- #


def test_hosts_ls_on_an_empty_inventory_tells_you_how_to_add_one(capsys):
    assert cli.main(["hosts", "ls"]) == cli.EXIT_OK
    assert "sleight hosts add" in capsys.readouterr().out


def test_hosts_add_then_ls_then_rm(capsys):
    assert cli.main([
        "hosts", "add", "hk-01", "--ssh", "deploy@1.2.3.4", "--dir", "/srv/cbm",
        "--port", "9100", "--sudo",
    ]) == cli.EXIT_OK
    capsys.readouterr()

    assert cli.main(["hosts", "ls"]) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "hk-01" in out and "deploy@1.2.3.4" in out

    # 一台没有部署记录的主机没法用，所以 add 顺带建了 default
    host = Store().require_host("hk-01")
    assert host.sudo
    assert Store().resolve("hk-01").spec.port == 9100

    assert cli.main(["hosts", "rm", "hk-01"]) == cli.EXIT_OK
    assert Store().hosts() == []
    assert Store().deployments() == []


def test_deployments_add_ls_rm(capsys):
    cli.main(["hosts", "add", "hk-01", "--ssh", "u@h", "--dir", "/srv/a", "--port", "9000"])
    capsys.readouterr()

    assert cli.main([
        "deployments", "add", "hk-01", "second", "--dir", "/srv/b", "--port", "9001",
    ]) == cli.EXIT_OK
    capsys.readouterr()

    assert cli.main(["deployments", "ls"]) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "hk-01/default" in out and "hk-01/second" in out

    assert cli.main(["deployments", "rm", "hk-01/second"]) == cli.EXIT_OK
    assert [d.name for d in Store().deployments()] == ["default"]


def test_a_second_deployment_on_one_host_gets_its_own_container(capsys):
    """不加后缀的话第二个 Manager 会把第一个的容器删掉 —— 真机上踩过。"""
    cli.main(["hosts", "add", "h", "--ssh", "u@h", "--dir", "/srv/a", "--port", "9000"])
    cli.main(["deployments", "add", "h", "second", "--dir", "/srv/b", "--port", "9001"])
    first, second = Store().deployments(host="h")
    assert first.spec.container_name != second.spec.container_name
    assert first.spec.name != second.spec.name


def test_history_records_what_happened(capsys, target):
    cli.main(["hosts", "add", "h", "--dir", "/srv/cbm", "--port", "9000"])
    capsys.readouterr()
    cli.main(["deploy", "--host", "h"])
    capsys.readouterr()
    assert cli.main(["history"]) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "deploy" in out and "成功" in out and "h/default" in out


def test_history_is_empty_at_first(capsys):
    assert cli.main(["history"]) == cli.EXIT_OK
    assert "还没有流水" in capsys.readouterr().out


def test_hosts_add_validates_before_saving(capsys):
    """存一个部署不了的配置，等于把错误推迟到最不方便的时刻。"""
    assert cli.main(["hosts", "add", "bad", "--ssh", "u@h", "--port", "0"]) == cli.EXIT_ERROR
    assert Store().deployments() == []


def test_hosts_rm_of_an_unknown_host(capsys):
    assert cli.main(["hosts", "rm", "nope"]) == cli.EXIT_ERROR


def test_hosts_subcommand_is_required(capsys):
    assert cli.main(["hosts"]) == cli.EXIT_USAGE
    assert "hosts ls|add|rm" in capsys.readouterr().err


def test_deployments_subcommand_is_required(capsys):
    assert cli.main(["deployments"]) == cli.EXIT_USAGE
    assert "deployments ls|add|rm" in capsys.readouterr().err


def test_ext_and_profiles_need_a_subcommand_too(capsys):
    assert cli.main(["ext"]) == cli.EXIT_USAGE
    assert cli.main(["profiles"]) == cli.EXIT_USAGE


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #


def test_table_aligns():
    text = cli._table([["a", "1"], ["bbb", "22"]], ["name", "n"])
    lines = text.splitlines()
    assert lines[0].startswith("name")
    assert all(len(line) == len(lines[0]) for line in lines)


def test_confirm_refuses_when_not_a_tty(capsys):
    assert not cli._confirm("delete?", "x", assume_yes=False)
    assert cli._confirm("delete?", "x", assume_yes=True)


# --------------------------------------------------------------------------- #
# 模板与建实例 —— 命令行和界面必须是同一套定义
# --------------------------------------------------------------------------- #


def test_templates_lists_both_kinds(capsys):
    assert cli.main(["templates"]) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "standard" in out and "private-net" in out
    assert "windows_us" in out and "America/New_York" in out
    assert "shm 5gb" in out                       # 说清每档到底是什么，而不是只有名字


def test_templates_json_carries_the_field_help(capsys):
    cli.main(["--json", "templates"])
    payload = json.loads(capsys.readouterr().out)
    assert {t["key"] for t in payload["deploy_templates"]} >= {"trial", "standard", "large"}
    assert payload["help"]["shm_size"]["recommend"]


def test_a_template_seeds_the_spec_and_flags_still_win(capsys):
    cli.main(["hosts", "add", "h", "--template", "large", "--dir", "/srv/a"])
    assert Store().resolve("h").spec.shm_size == "8gb"

    cli.main(["deployments", "add", "h", "second", "--template", "large",
              "--dir", "/srv/b", "--port", "9001", "--shm-size", "2gb"])
    assert Store().resolve("h/second").spec.shm_size == "2gb", "命令行显式给的必须压过模板"


def test_the_private_net_template_carries_its_own_expose_flag():
    """它把 bind_ip 设成 0.0.0.0，没有 expose=True 的话连存都存不进去。"""
    cli.main(["hosts", "add", "h", "--template", "private-net", "--dir", "/srv/a"])
    spec = Store().resolve("h").spec
    assert spec.bind_ip == "0.0.0.0" and spec.expose is True


def test_an_unknown_template_lists_the_real_ones(capsys):
    """argparse 的 choices 直接拦掉并把合法值列出来 —— 比运行到一半再报错好。"""
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["hosts", "add", "h", "--template", "nope", "--dir", "/srv/a"])
    assert exit_info.value.code == 2
    err = capsys.readouterr().err
    assert "invalid choice" in err
    assert "standard" in err and "private-net" in err


def test_profiles_create_uses_the_preset(capsys, target, monkeypatch):
    """身份模板保证平台/时区/语言/GPU 自洽 —— 建实例时就该定下来。"""
    from contextlib import contextmanager

    seen = {}

    class FakeManager:
        name = "fake"

        def ensure_profile(self, spec):
            seen["spec"] = spec
            from sleight.core.types import InstanceInfo
            return InstanceInfo(id="p1", provider="fake", ready=False,
                                name=spec.name, tags=frozenset(spec.tags))

    @contextmanager
    def connect(**_kw):
        yield FakeManager()

    monkeypatch.setattr(cli.Deployer, "connect", lambda self, **kw: connect())
    cli.main(["hosts", "add", "h", "--dir", "/srv/cbm"])
    capsys.readouterr()

    assert cli.main([
        "profiles", "create", "news-hk-01", "--host", "h", "--preset", "windows_hk",
        "--tags", "hk,news", "--screen", "1366x768",
    ]) == cli.EXIT_OK

    spec = seen["spec"]
    assert spec.platform == "windows" and spec.timezone == "Asia/Hong_Kong"
    assert spec.locale == "zh-HK"
    assert "Intel" in spec.gpu_renderer          # 和 platform 自洽
    assert spec.tags == ("hk", "news")
    assert (spec.screen_width, spec.screen_height) == (1366, 768)
    assert spec.auto_launch is False, "建实例不该顺手拉起一个浏览器占内存"
    assert "news-hk-01" in capsys.readouterr().out


def test_profiles_create_refuses_a_contradictory_combination(capsys, target):
    """geoip 要靠代理出口 IP 推导，没代理就什么都推不出来。"""
    cli.main(["hosts", "add", "h", "--dir", "/srv/cbm"])
    capsys.readouterr()
    assert cli.main(["profiles", "create", "x", "--host", "h", "--geoip"]) == cli.EXIT_ERROR
    assert "geoip" in capsys.readouterr().err


def test_a_closed_output_pipe_is_not_a_traceback(capsys, monkeypatch):
    """`sleight templates | head -3` 不该吐一段 BrokenPipeError。

    下游关管道是正常的。不处理的话解释器退出时还会往已关闭的 fd 上刷缓冲，
    再打一段 "Exception ignored" 的噪音。
    """
    def explode(*_a, **_kw):
        raise BrokenPipeError(32, "Broken pipe")

    monkeypatch.setattr(cli, "_dump", explode)
    assert cli.main(["--json", "templates"]) == cli.EXIT_SIGPIPE


def test_a_local_deploy_hands_out_the_local_url_not_the_tunnel_port(capsys, target):
    """写死 19000 的话，本机用户会照着这段去连一个根本没人监听的端口。

    而同一屏上面两行刚说过是 9000 —— 自相矛盾的成功输出比报错还难查。
    """
    cli.main(["deploy", "--port", "9000"])
    out = capsys.readouterr().out
    assert 'CloakBrowserManager("http://127.0.0.1:9000")' in out
    assert "19000" not in out, "本机部署不该出现隧道端口"


def test_a_remote_deploy_hands_out_the_tunnel_port(capsys, target):
    """反过来：远程时 Manager 只绑在目标机的 127.0.0.1，本地端口才是能连的那个。

    和上一条一起把两个分支都钉住 —— 只测本机的话，把整个条件删成 local_url 也照样绿。
    """
    target.target = "deploy@10.0.0.12"                    # 让 _tunnel_hint 认为是远程
    cli.main(["deploy", "--port", "9000"])
    out = capsys.readouterr().out
    assert 'CloakBrowserManager("http://127.0.0.1:19000")' in out
    assert '"http://127.0.0.1:9000")' not in out, "远程给的是目标机端口，本地连不上"


def test_history_accepts_host_as_well_as_only_host(capsys, target):
    """别的子命令全是 --host，连敲五遍之后第六遍还会敲它。"""
    cli.main(["hosts", "add", "hk", "--ssh", "u@h", "--dir", "/srv/a"])
    cli.main(["deploy", "--host", "hk"])
    capsys.readouterr()

    assert cli.main(["history", "--host", "hk"]) == cli.EXIT_OK
    by_host = capsys.readouterr().out
    assert cli.main(["history", "--only-host", "hk"]) == cli.EXIT_OK
    assert capsys.readouterr().out == by_host
