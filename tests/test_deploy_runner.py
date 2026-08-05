"""Runner：本机执行、SSH 命令拼装、原子写、目录整体替换。

SSH 部分**不连网络** —— 打桩 ``subprocess.run`` 之后断言 argv。argv 才是这一层唯一
会出错的东西：引用错了就是注入面，选项漏了就是挂在密码提示前。
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from sleight.deploy import runner as runner_mod
from sleight.deploy.errors import CommandFailed, DeployError
from sleight.deploy.runner import CommandResult, LocalRunner, SSHRunner, describe

PY = sys.executable


# --------------------------------------------------------------------------- #
# CommandResult
# --------------------------------------------------------------------------- #


def test_nonzero_does_not_raise_by_itself():
    """很多探测就是靠非 0 判断"没有"，自动抛异常会让它们全变成异常控制流。"""
    result = CommandResult(("test", "-d", "/nope"), 1)
    assert not result.ok
    with pytest.raises(CommandFailed, match="exit 1"):
        result.check()


def test_check_puts_stderr_in_the_message():
    """远程失败时没有 stderr 等于让人瞎猜。"""
    result = CommandResult(("docker", "compose", "up"), 1, "", "no such image: nope\n")
    with pytest.raises(CommandFailed, match="no such image"):
        result.check()


# --------------------------------------------------------------------------- #
# LocalRunner
# --------------------------------------------------------------------------- #


def test_local_run_captures_output():
    r = LocalRunner().run([PY, "-c", "print('hi')"])
    assert r.ok and r.text == "hi"


def test_local_missing_binary_is_127_not_an_exception():
    r = LocalRunner().run(["definitely-not-a-real-binary-xyz"])
    assert r.code == 127
    assert "not found" in r.err


def test_local_timeout_is_124():
    r = LocalRunner().run([PY, "-c", "import time; time.sleep(30)"], timeout=1.0)
    assert r.code == 124
    assert "timed out" in r.err


def test_local_stdin_reaches_the_process():
    r = LocalRunner().run([PY, "-c", "import sys; print(sys.stdin.read().upper())"], stdin=b"abc")
    assert r.text == "ABC"


def test_local_put_text_is_atomic_and_respects_mode(tmp_path):
    target = tmp_path / "sub" / ".env"
    LocalRunner().put_text("AUTH_TOKEN=x\n", str(target), mode=0o600)
    assert target.read_text() == "AUTH_TOKEN=x\n"
    assert not list(tmp_path.rglob("*.sleight-tmp"))
    if os.name != "nt":                      # Windows 没有 POSIX 权限位
        assert oct(target.stat().st_mode)[-3:] == "600"


def test_local_read_text_missing_is_none_not_an_error(tmp_path):
    """调用点几乎全是"有就沿用、没有就新建"，None 比异常好用。"""
    assert LocalRunner().read_text(str(tmp_path / "nope")) is None
    assert LocalRunner().read_text(str(tmp_path)) is None      # 目录也算没有


def test_local_put_dir_removes_files_the_new_version_dropped(tmp_path):
    """等价于 rsync --delete：旧版本删掉的文件不能残留。"""
    src, dst = tmp_path / "src", tmp_path / "dst"
    src.mkdir()
    (src / "new.js").write_text("new")
    dst.mkdir()
    (dst / "stale.js").write_text("stale")

    LocalRunner().put_dir(str(src), str(dst))
    assert (dst / "new.js").read_text() == "new"
    assert not (dst / "stale.js").exists()
    assert not list(tmp_path.glob("*.sleight-new"))


def test_local_tunnel_is_a_no_op():
    """本机部署本来就连得上，所以调用方对本机/远程可以写同一段代码。"""
    with LocalRunner().tunnel(9000) as port:
        assert port == 9000


def test_local_stream_returns_exit_code():
    assert LocalRunner().stream([PY, "-c", "raise SystemExit(3)"]) == 3


# --------------------------------------------------------------------------- #
# SSHRunner —— 只看 argv
# --------------------------------------------------------------------------- #


class Recorder:
    """记下 subprocess.run 收到的 argv 和 stdin。"""

    def __init__(self, code: int = 0, out: bytes = b"", err: bytes = b"") -> None:
        self.calls: list[tuple[list[str], bytes | None]] = []
        self._reply = (code, out, err)

    def __call__(self, argv, *, input=None, capture_output=None, timeout=None, cwd=None):
        self.calls.append((list(argv), input))
        code, out, err = self._reply
        return subprocess.CompletedProcess(argv, code, out, err)

    @property
    def argv(self) -> list[str]:
        return self.calls[-1][0]

    @property
    def remote(self) -> str:
        """最后一个参数就是发给远端 shell 的那一整串。"""
        return self.calls[-1][0][-1]


@pytest.fixture
def rec(monkeypatch):
    recorder = Recorder()
    monkeypatch.setattr(runner_mod.subprocess, "run", recorder)
    return recorder


def test_ssh_argv_has_the_options_that_prevent_hangs(rec):
    SSHRunner("deploy@host").run(["true"])
    argv = rec.argv
    assert argv[0] == "ssh"
    # BatchMode 让缺 key 时当场失败，而不是挂在一个看不见的密码提示前
    assert "BatchMode=yes" in argv
    assert "ConnectTimeout=15" in argv
    assert "ServerAliveInterval=30" in argv
    assert "-T" in argv
    assert argv[-2] == "deploy@host"


def test_ssh_identity_and_port(rec):
    SSHRunner("deploy@host", port=2222, identity="/tmp/key").run(["true"])
    argv = rec.argv
    assert argv[argv.index("-i") + 1] == "/tmp/key"
    assert argv[argv.index("-p") + 1] == "2222"


def test_ssh_reuses_one_connection_on_posix(rec):
    """一次部署要发几十条命令，没有 ControlMaster 就是几十次 TCP + 认证。"""
    SSHRunner("deploy@host").run(["true"])
    joined = " ".join(rec.argv)
    if os.name == "nt":
        assert "ControlMaster" not in joined    # Windows OpenSSH 不支持
    else:
        assert "ControlMaster=auto" in joined
        assert "ControlPersist=60s" in joined


def test_ssh_batch_can_be_turned_off_for_password_auth(rec):
    SSHRunner("deploy@host", batch=False).run(["true"])
    assert "BatchMode=yes" not in rec.argv


def test_ssh_host_key_policy_defaults_to_the_users_own_config(rec):
    SSHRunner("deploy@host").run(["true"])
    assert "StrictHostKeyChecking" not in " ".join(rec.argv)
    SSHRunner("deploy@host", strict_host_key="accept-new").run(["true"])
    assert "StrictHostKeyChecking=accept-new" in " ".join(rec.argv)


def test_remote_command_is_quoted_not_concatenated(rec):
    """调用方给的是 argv 数组，引用由这一层统一做 —— 否则带空格的路径就是注入面。"""
    SSHRunner("deploy@host").run(["cat", "/srv/my dir/.env"])
    assert rec.remote == "cat '/srv/my dir/.env'"


def test_single_quotes_in_a_path_survive(rec):
    SSHRunner("deploy@host").run(["cat", "/srv/o'brien/.env"])
    assert rec.remote == "cat '/srv/o'\"'\"'brien/.env'"


def test_cwd_becomes_a_cd_prefix(rec):
    SSHRunner("deploy@host").run(["docker", "compose", "up", "-d"], cwd="/srv/cbm")
    assert rec.remote == "cd /srv/cbm && docker compose up -d"


def test_sudo_is_non_interactive(rec):
    """交互式 sudo 在 SSH 上会挂死在一个看不见的提示符前。"""
    SSHRunner("deploy@host").run(["mkdir", "-p", "/srv/cbm"], sudo=True)
    assert rec.remote == "sudo -n mkdir -p /srv/cbm"


def test_put_text_sends_content_on_stdin_never_in_argv(rec):
    """.env 里有 token。进了 argv 就会出现在目标机的进程列表里。"""
    SSHRunner("deploy@host").put_text("AUTH_TOKEN=secret\n", "/srv/cbm/.env", mode=0o600)
    scripts = [call[0][-1] for call in rec.calls]
    stdins = [call[1] for call in rec.calls]
    assert b"AUTH_TOKEN=secret\n" in stdins
    assert not any("secret" in s for s in scripts)
    # 先写临时文件再 mv：中途断线不会留下半个 .env
    assert scripts[0] == "sh -c 'cat > /srv/cbm/.env.sleight-tmp'"
    assert scripts[1] == "chmod 600 /srv/cbm/.env.sleight-tmp"
    assert scripts[2] == "mv -f /srv/cbm/.env.sleight-tmp /srv/cbm/.env"


def test_put_dir_stages_then_swaps(rec, tmp_path):
    """整体替换而不是增量覆盖：不存在"浏览器读到半个插件目录"的窗口。"""
    src = tmp_path / "ext"
    src.mkdir()
    (src / "manifest.json").write_text("{}")

    SSHRunner("deploy@host").put_dir(str(src), "/data/extensions/ext")
    extract, swap = rec.calls[0][0][-1], rec.calls[1][0][-1]
    assert "rm -rf /data/extensions/ext.sleight-new" in extract
    assert "tar -xzf - -C /data/extensions/ext.sleight-new" in extract
    assert swap == (
        "sh -c 'rm -rf /data/extensions/ext && "
        "mv /data/extensions/ext.sleight-new /data/extensions/ext'"
    )
    assert rec.calls[0][1], "tar 应该走 stdin"


def test_permission_denied_says_batchmode_is_why(monkeypatch):
    """255 + permission denied 是"key 没配好"，给出下一步比抛 exit 255 有用。"""
    monkeypatch.setattr(
        runner_mod.subprocess, "run", Recorder(255, b"", b"deploy@host: Permission denied (publickey).")
    )
    with pytest.raises(DeployError, match="ssh-copy-id"):
        SSHRunner("deploy@host").run(["true"])


def test_missing_ssh_binary_is_explained(monkeypatch):
    def boom(*a, **kw):
        raise FileNotFoundError("ssh")

    monkeypatch.setattr(runner_mod.subprocess, "run", boom)
    with pytest.raises(DeployError, match="openssh-client"):
        SSHRunner("deploy@host").run(["true"])


def test_target_must_not_look_like_an_option():
    with pytest.raises(DeployError):
        SSHRunner("-oProxyCommand=evil")


def test_tunnel_failure_reports_ssh_stderr(monkeypatch):
    """ExitOnForwardFailure 让 ssh 直接退出；不盯进程就会白等满超时。"""

    class DeadProc:
        returncode = 1

        def __init__(self, *a, **kw):
            class Err:
                @staticmethod
                def read():
                    return b"bind: Address already in use\n"

            self.stderr = Err()

        def poll(self):
            return 1

        def terminate(self):
            pass

        def wait(self, timeout=None):
            return 1

    monkeypatch.setattr(runner_mod.subprocess, "Popen", DeadProc)
    with pytest.raises(DeployError, match="Address already in use"), SSHRunner(
        "deploy@host"
    ).tunnel(9000):
        pass


# --------------------------------------------------------------------------- #
# 打包
# --------------------------------------------------------------------------- #


def test_tar_is_byte_identical_for_identical_content(tmp_path):
    """归一化 uid/mtime，所以"内容没变就不用重传"这种判断才成立。"""
    for name in ("a", "b"):
        root = tmp_path / name
        (root / "sub").mkdir(parents=True)
        (root / "manifest.json").write_text('{"manifest_version": 3}')
        (root / "sub" / "bg.js").write_text("x")
    assert runner_mod._tar_gz(tmp_path / "a") == runner_mod._tar_gz(tmp_path / "b")


def test_tar_refuses_a_file(tmp_path):
    target = tmp_path / "f.txt"
    target.write_text("x")
    with pytest.raises(DeployError, match="not a directory"):
        runner_mod._tar_gz(target)


def test_describe_is_pasteable():
    assert describe(["cat", "/a b/c"]) == "cat '/a b/c'"


def test_the_control_socket_dir_is_stable_not_per_process():
    """每进程一个临时目录会在 /tmp 里越积越多，而且跨命令没法复用连接。"""
    import os

    if os.name == "nt":
        pytest.skip("Windows OpenSSH 不支持 ControlMaster")
    first = runner_mod._control_dir()
    assert runner_mod._control_dir() == first
    assert str(os.getuid()) in first
    assert oct(os.stat(first).st_mode)[-3:] == "700"


def test_two_runners_share_the_control_dir(rec):
    SSHRunner("a@h").run(["true"])
    first = [a for a in rec.argv if a.startswith("ControlPath=")]
    SSHRunner("b@h").run(["true"])
    second = [a for a in rec.argv if a.startswith("ControlPath=")]
    if os.name != "nt":
        assert first == second and first


def test_local_file_ops_never_shell_out(tmp_path, monkeypatch):
    """本机读写和复制必须走原生 os/shutil，不能走 ``sh -c``。

    这条是 Windows 上炸出来的：那边的路径带反斜杠和盘符，塞进 shlex.quote + sh
    会被搅成 ``C\\:\\\\Users\\...``，tar 报 "Cannot open"。而且 Windows 上本来就
    没有 POSIX shell 可以假定存在。
    """
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.js").write_text("x")

    runner = LocalRunner()
    shelled: list[tuple] = []
    monkeypatch.setattr(
        runner, "run", lambda *a, **kw: shelled.append(a) or CommandResult((), 0)
    )

    runner.put_text("hi", str(tmp_path / "f.txt"), mode=0o600)
    runner.put_dir(str(src), str(tmp_path / "dst"))
    assert runner.read_text(str(tmp_path / "f.txt")) == "hi"

    assert shelled == [], f"这些本该是原生调用，却走了 shell: {shelled}"
    assert (tmp_path / "dst" / "a.js").read_text() == "x"
