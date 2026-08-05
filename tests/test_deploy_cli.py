"""主机清单与命令行。

CLI 的测法：把 ``_resolve`` 换成一个 :class:`FakeRunner`，然后断言**退出码**和**输出**。
退出码是脚本唯一能依赖的契约（0 成功 / 1 失败 / 2 用法 / 3 体检没过），所以每一种都测。
"""

from __future__ import annotations

import json

import pytest

from sleight import cli
from sleight.deploy.errors import DeployError
from sleight.deploy.inventory import Host, Inventory, sleight_home
from sleight.deploy.render import parse_env
from sleight.deploy.spec import DeploySpec

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
        spec = SPEC
        for flag, field in cli._SPEC_FLAGS.items():
            value = getattr(args, flag, None)
            if value is not None:
                spec = spec.replace(**{field: value})
        return runner, spec, bool(getattr(args, "sudo", False))

    monkeypatch.setattr(cli, "_resolve", resolve)
    return runner


# --------------------------------------------------------------------------- #
# 清单
# --------------------------------------------------------------------------- #


def test_home_follows_the_env_var(isolated_home):
    assert sleight_home() == isolated_home


def test_inventory_round_trip():
    inv = Inventory.load()
    inv.add(Host(
        name="hk-01", ssh="deploy@1.2.3.4", port=2222, identity="~/.ssh/id_ed25519",
        sudo=True, strict_host_key="accept-new",
        deploy={"dir": "/srv/cbm", "port": 9100, "expose": True},
    ))
    path = inv.save()
    assert path.read_text().count("[hosts.hk-01") == 2      # 主表 + deploy 子表

    host = Inventory.load().get("hk-01")
    assert host.ssh == "deploy@1.2.3.4"
    assert host.port == 2222
    assert host.sudo is True
    assert host.strict_host_key == "accept-new"
    assert host.spec().port == 9100
    assert host.spec().dir == "/srv/cbm"


def test_inventory_never_writes_a_token():
    """能 SSH 上去就能读到目标机的 .env，控制机上再存一份只是多一个泄漏点。"""
    inv = Inventory.load()
    inv.add(Host(name="a", ssh="u@h", deploy={"dir": "/srv/x"}))
    text = inv.save().read_text()
    assert "token" not in text.lower() or "AUTH_TOKEN 在它自己的 .env" in text


def test_missing_file_is_an_empty_inventory():
    assert Inventory.load().hosts == {}


def test_unknown_host_lists_the_known_ones():
    inv = Inventory.load()
    inv.add(Host(name="hk-01", ssh="u@h"))
    inv.save()
    with pytest.raises(DeployError, match="hk-01"):
        Inventory.load().get("sg-02")


def test_broken_toml_is_reported_not_ignored():
    """静默忽略会让人以为主机没配上，然后在错误的方向排半天。"""
    path = sleight_home()
    path.mkdir(parents=True, exist_ok=True)
    (path / "hosts.toml").write_text("[hosts.a\nssh =")
    with pytest.raises(DeployError, match="valid TOML"):
        Inventory.load()


def test_odd_host_names_get_quoted():
    inv = Inventory.load()
    inv.add(Host(name="prod.hk 01", ssh="u@h"))
    inv.save()
    assert Inventory.load().get("prod.hk 01").ssh == "u@h"


def test_empty_host_means_local():
    assert Host(name="local").local
    assert not Host(name="x", ssh="u@h").local


def test_host_spec_ignores_none_overrides():
    """命令行没给的参数不该把配置文件里的值打掉。"""
    host = Host(name="a", deploy={"port": 9100})
    assert host.spec(port=None, dir=None).port == 9100
    assert host.spec(port=9200).port == 9200


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
    assert "hk-01" in out and "deploy@1.2.3.4" in out and "9100" in out

    host = Inventory.load().get("hk-01")
    assert host.sudo and host.spec().port == 9100

    assert cli.main(["hosts", "rm", "hk-01"]) == cli.EXIT_OK
    assert Inventory.load().hosts == {}


def test_hosts_add_validates_before_saving(capsys):
    """存一个部署不了的配置，等于把错误推迟到最不方便的时刻。"""
    assert cli.main(["hosts", "add", "bad", "--ssh", "u@h", "--port", "0"]) == cli.EXIT_ERROR
    assert Inventory.load().hosts == {}


def test_hosts_rm_of_an_unknown_host(capsys):
    assert cli.main(["hosts", "rm", "nope"]) == cli.EXIT_ERROR


def test_hosts_subcommand_is_required(capsys):
    assert cli.main(["hosts"]) == cli.EXIT_USAGE
    assert "hosts ls|add|rm" in capsys.readouterr().err


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
