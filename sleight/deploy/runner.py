"""在某台机器上执行命令、读写文件、开隧道。

**整个部署方案的支点。** ``Deployer`` 只认 :class:`Runner`，所以"本机 docker 就地
部署"和"SSH 到另一台部署"不是两套实现，是同一套代码换一个 Runner —— 行为因此一定
一致，不会出现本机能跑远程跑不通的分裂。

远程用系统 ``ssh`` 二进制而不是 paramiko：白拿 ``~/.ssh/config``、``ProxyJump``、
ssh-agent、``known_hosts``，Windows 10+ 也自带 OpenSSH。零依赖。

**命令一律是 argv 数组**，跨到远程时才由这里统一 ``shlex.quote``。调用方永远不需要
自己拼 shell 字符串，也就没有注入面。
"""

from __future__ import annotations

import io
import logging
import os
import shlex
import shutil
import socket
import subprocess
import tarfile
import tempfile
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

from .errors import CommandFailed, DeployError

log = logging.getLogger("sleight.deploy.runner")

__all__ = ["CommandResult", "LocalRunner", "Runner", "SSHRunner", "describe"]

DEFAULT_TIMEOUT = 120.0
#: 拉镜像可能是几个 GB，不能按普通命令的超时算
PULL_TIMEOUT = 1800.0
#: put_dir 是全量打包进内存再送走的，超过这个体积说明传错目录了
MAX_DIR_BYTES = 256 * 1024 * 1024


def describe(argv: Sequence[str]) -> str:
    """把 argv 渲染成可粘贴执行的一行，给 ``--dry-run`` 和日志用。"""
    return shlex.join(argv)


@dataclass(frozen=True, slots=True)
class CommandResult:
    """一条命令的结果。**非 0 不自动抛异常** —— 很多探测就是靠非 0 判断"没有"。"""

    argv: tuple[str, ...]
    code: int
    out: str = ""
    err: str = field(default="", repr=False)

    @property
    def ok(self) -> bool:
        return self.code == 0

    @property
    def text(self) -> str:
        """stdout 去掉首尾空白。"""
        return self.out.strip()

    def check(self) -> CommandResult:
        """非 0 就抛。

        :raises CommandFailed: 消息里带 stderr —— 远程排错时没有它等于瞎猜
        """
        if self.ok:
            return self
        detail = (self.err.strip() or self.out.strip() or "(no output)").splitlines()
        tail = "\n  ".join(detail[-8:])
        raise CommandFailed(f"exit {self.code}: {describe(self.argv)}\n  {tail}", self)


@runtime_checkable
class Runner(Protocol):
    """在某台机器上干活的最小接口。"""

    label: str

    def run(
        self,
        argv: Sequence[str],
        *,
        stdin: bytes | None = ...,
        cwd: str | None = ...,
        timeout: float | None = ...,
        sudo: bool = ...,
        check: bool = ...,
    ) -> CommandResult: ...

    def read_text(self, path: str, *, sudo: bool = ...) -> str | None: ...
    def put_text(self, text: str, path: str, *, mode: int | None = ..., sudo: bool = ...) -> None: ...
    def put_dir(self, local: str, remote: str, *, sudo: bool = ...) -> None: ...
    def stream(self, argv: Sequence[str], *, cwd: str | None = ...) -> int: ...
    def close(self) -> None: ...


class _BaseRunner:
    """两个 Runner 共用的、只依赖 :meth:`run` 的实现。"""

    label = "?"

    def run(self, argv, **kw) -> CommandResult:      # pragma: no cover - 子类实现
        raise NotImplementedError

    # ------------------------------------------------------------------ #

    def exists(self, path: str, *, sudo: bool = False) -> bool:
        """目标机上这个路径存在吗。"""
        return self.run(["test", "-e", path], sudo=sudo).ok

    def mkdir(self, path: str, *, sudo: bool = False) -> None:
        self.run(["mkdir", "-p", path], sudo=sudo, check=True)

    def read_text(self, path: str, *, sudo: bool = False) -> str | None:
        """读一个文本文件。**不存在返回 ``None``**，不抛 —— 调用点几乎全是
        "有就沿用、没有就新建"。"""
        r = self.run(["cat", path], sudo=sudo)
        return r.out if r.ok else None

    def put_text(self, text: str, path: str, *, mode: int | None = None, sudo: bool = False) -> None:
        """原子地写一个文件。

        先写同目录的临时文件再 ``mv``：中途断线不会留下半个 compose 文件，更不会留下
        半个 ``.env``（那会让下一次 ``up`` 拉起一个空 token 的 Manager）。

        内容走 **stdin**，不进 argv —— ``.env`` 里有 token，进了 argv 就会出现在目标机
        的进程列表和 shell 历史里。
        """
        tmp = f"{path}.sleight-tmp"
        data = text.encode()
        self.run(["sh", "-c", f"cat > {shlex.quote(tmp)}"], stdin=data, sudo=sudo, check=True)
        if mode is not None:
            self.run(["chmod", f"{mode:o}", tmp], sudo=sudo, check=True)
        self.run(["mv", "-f", tmp, path], sudo=sudo, check=True)

    def put_dir(self, local: str, remote: str, *, sudo: bool = False) -> None:
        """把本地目录整个换到目标机的 ``remote``。

        打成 tar.gz 走 stdin，落到 ``remote.sleight-new`` 再整体替换。**不是增量同步**：
        旧版本删掉的文件不会残留（等价于 ``rsync --delete``），而且切换是一次 ``mv``，
        不存在"浏览器读到半个插件目录"的窗口。

        用 :mod:`tarfile` 在内存里打包而不是调 ``rsync``/``tar``：控制机可能是 Windows，
        也可能两边的 rsync 版本对不上。
        """
        blob = _tar_gz(Path(local))
        staging = f"{remote}.sleight-new"
        script = (
            f"rm -rf {shlex.quote(staging)} && mkdir -p {shlex.quote(staging)} "
            f"&& tar -xzf - -C {shlex.quote(staging)}"
        )
        self.run(["sh", "-c", script], stdin=blob, sudo=sudo, timeout=600, check=True)
        swap = (
            f"rm -rf {shlex.quote(remote)} && mv {shlex.quote(staging)} {shlex.quote(remote)}"
        )
        self.run(["sh", "-c", swap], sudo=sudo, check=True)

    @contextmanager
    def tunnel(self, remote_port: int, *, remote_host: str = "127.0.0.1") -> Iterator[int]:
        """让控制机能连到目标机上的 ``remote_port``，返回本地端口。

        本机 Runner 什么也不做（本来就连得上）；SSH Runner 开一条临时端口转发。
        调用方因此可以对两种情况写同一段代码。
        """
        yield remote_port

    def close(self) -> None:
        return None

    def __enter__(self):
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class LocalRunner(_BaseRunner):
    """在本机执行。目标机 = 控制机。

    文件读写走原生 ``os`` 调用而不是 ``sh -c cat``：Windows 上跑单测时没有 POSIX
    shell，而这两个方法是被测得最多的。
    """

    def __init__(self, *, sudo_prefix: Sequence[str] = ("sudo", "-n")) -> None:
        self.label = "local"
        self._sudo = tuple(sudo_prefix)

    def run(
        self,
        argv: Sequence[str],
        *,
        stdin: bytes | None = None,
        cwd: str | None = None,
        timeout: float | None = None,
        sudo: bool = False,
        check: bool = False,
    ) -> CommandResult:
        full = (*self._sudo, *argv) if sudo else tuple(argv)
        log.debug("local: %s", describe(full))
        try:
            proc = subprocess.run(                       # argv 数组，不经过 shell
                full,
                input=stdin,
                cwd=cwd,
                capture_output=True,
                timeout=timeout or DEFAULT_TIMEOUT,
            )
        except FileNotFoundError as exc:
            result = CommandResult(full, 127, "", f"{argv[0]}: command not found ({exc})")
        except subprocess.TimeoutExpired as exc:
            result = CommandResult(full, 124, _decode(exc.stdout), f"timed out after {exc.timeout}s")
        else:
            result = CommandResult(full, proc.returncode, _decode(proc.stdout), _decode(proc.stderr))
        return result.check() if check else result

    def stream(self, argv: Sequence[str], *, cwd: str | None = None) -> int:
        return subprocess.call(list(argv), cwd=cwd)   # argv 数组，不经过 shell

    def read_text(self, path: str, *, sudo: bool = False) -> str | None:
        if sudo:
            return super().read_text(path, sudo=True)
        try:
            return Path(path).read_text(encoding="utf-8")
        except (FileNotFoundError, NotADirectoryError, IsADirectoryError):
            return None
        except PermissionError:
            return super().read_text(path, sudo=True)

    def put_text(self, text: str, path: str, *, mode: int | None = None, sudo: bool = False) -> None:
        if sudo:
            super().put_text(text, path, mode=mode, sudo=True)
            return
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".sleight-tmp")
        # 先建成 600 再写，避免 token 有一瞬间是 644
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode if mode is not None else 0o644)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, target)

    def put_dir(self, local: str, remote: str, *, sudo: bool = False) -> None:
        if sudo or os.name == "nt":
            super().put_dir(local, remote, sudo=sudo)
            return
        src, dst = Path(local), Path(remote)
        staging = dst.with_name(dst.name + ".sleight-new")
        if staging.exists():
            shutil.rmtree(staging)
        shutil.copytree(src, staging)
        if dst.exists():
            shutil.rmtree(dst)
        staging.replace(dst)


class SSHRunner(_BaseRunner):
    """通过系统 ``ssh`` 在远程机器上执行。

    :param target: ``user@host`` 或 ``~/.ssh/config`` 里的别名
    :param port: SSH 端口
    :param identity: 私钥路径。不给就交给 ssh-agent / ssh_config
    :param batch: 默认 ``True``，即 ``BatchMode=yes`` —— 需要交互输密码时**当场失败**
        而不是挂在一个看不见的提示符前。用密码认证的话传 ``False``
    :param strict_host_key: 直接映射到 ``StrictHostKeyChecking``。``None`` 表示尊重
        用户自己的 ssh 配置，不替他做主
    :param options: 额外的 ``-o`` 选项
    """

    def __init__(
        self,
        target: str,
        *,
        port: int | None = None,
        identity: str | None = None,
        batch: bool = True,
        strict_host_key: str | None = None,
        options: Sequence[str] = (),
        sudo_prefix: Sequence[str] = ("sudo", "-n"),
    ) -> None:
        if not target or target.startswith("-"):
            raise DeployError(f"invalid ssh target {target!r}")
        self.target = target
        self.label = target
        self.port = port
        self.identity = os.path.expanduser(identity) if identity else None
        self.batch = batch
        self.strict_host_key = strict_host_key
        self.options = tuple(options)
        self._sudo = tuple(sudo_prefix)

    # ------------------------------------------------------------------ #

    def _control_args(self) -> list[str]:
        """连接复用。一次部署要发几十条命令，没有它就是几十次 TCP + 认证。

        复用目录是**每用户固定的一个**，不是每次 new 一个临时目录：
        每进程一个的话，一次 CLI 调用就在 /tmp 里留下一个再也不会被用到的空目录，而且
        ``sleight status`` 紧接着 ``sleight ext ls`` 也没法共用连接。固定目录 +
        ``ControlPersist=60s`` 让**跨命令**也能复用。

        Windows 版 OpenSSH 不支持 ControlMaster（没有 Unix socket），那里直接关掉。
        """
        if os.name == "nt":
            return []
        # socket 路径有 ~104 字节上限，%C 是固定长度的哈希，不用主机名拼
        return [
            "-o", "ControlMaster=auto",
            "-o", f"ControlPath={_control_dir()}/%C",
            "-o", "ControlPersist=60s",
        ]

    def _ssh_argv(self, *, tty: bool = False) -> list[str]:
        argv = ["ssh"]
        if self.identity:
            argv += ["-i", self.identity]
        if self.port:
            argv += ["-p", str(self.port)]
        if self.batch:
            argv += ["-o", "BatchMode=yes"]
        if self.strict_host_key:
            argv += ["-o", f"StrictHostKeyChecking={self.strict_host_key}"]
        argv += [
            "-o", "ConnectTimeout=15",
            "-o", "ServerAliveInterval=30",
            "-o", "ServerAliveCountMax=3",
        ]
        argv += self._control_args()
        for opt in self.options:
            argv += ["-o", opt]
        if not tty:
            argv.append("-T")
        return argv

    def run(
        self,
        argv: Sequence[str],
        *,
        stdin: bytes | None = None,
        cwd: str | None = None,
        timeout: float | None = None,
        sudo: bool = False,
        check: bool = False,
    ) -> CommandResult:
        remote = (*self._sudo, *argv) if sudo else tuple(argv)
        command = shlex.join(remote)
        if cwd:
            command = f"cd {shlex.quote(cwd)} && {command}"
        full = tuple([*self._ssh_argv(), self.target, command])
        log.debug("%s: %s", self.target, command)
        try:
            proc = subprocess.run(                       # argv 数组，不经过 shell
                full,
                input=stdin,
                capture_output=True,
                timeout=timeout or DEFAULT_TIMEOUT,
            )
        except FileNotFoundError:
            raise DeployError(
                "no 'ssh' binary on PATH; sleight drives the system OpenSSH client "
                "(Windows 10+ ships one, or install openssh-client)"
            ) from None
        except subprocess.TimeoutExpired as exc:
            result = CommandResult(full, 124, _decode(exc.stdout), f"timed out after {exc.timeout}s")
        else:
            result = CommandResult(full, proc.returncode, _decode(proc.stdout), _decode(proc.stderr))
            if result.code == 255 and "ermission denied" in result.err:
                raise DeployError(
                    f"ssh to {self.target} was refused: {result.err.strip().splitlines()[-1]}\n"
                    "BatchMode is on, so no password prompt was shown. Fix the key "
                    "(ssh-copy-id / ssh-agent) or pass batch=False for password auth."
                )
        # 255 是 ssh 自己的错误码，和远程命令的退出码撞不上（远程返回 255 的很少见）
        return result.check() if check else result

    def stream(self, argv: Sequence[str], *, cwd: str | None = None) -> int:
        """输出直接接到当前终端。给 ``logs -f`` 这种要一直看的命令用。"""
        command = shlex.join(argv)
        if cwd:
            command = f"cd {shlex.quote(cwd)} && {command}"
        return subprocess.call(                           # argv 数组，不经过 shell
            [*self._ssh_argv(tty=True), self.target, command]
        )

    # ------------------------------------------------------------------ #

    @contextmanager
    def tunnel(self, remote_port: int, *, remote_host: str = "127.0.0.1") -> Iterator[int]:
        """临时的本地端口转发，等价于手册 A.5 里那条 ``ssh -N -L``。

        Manager 通常只绑在目标机的 ``127.0.0.1`` 上，控制机想直接调它的 HTTP API
        就得先有这条隧道。用完即拆。

        :param remote_port: 目标机上的端口
        :returns: 控制机上的本地端口（上下文内有效）
        """
        local_port = _free_port()
        argv = [
            *self._ssh_argv(),
            "-N",
            "-o", "ExitOnForwardFailure=yes",
            "-L", f"127.0.0.1:{local_port}:{remote_host}:{remote_port}",
            self.target,
        ]
        log.debug("tunnel: %s", describe(argv))
        proc = subprocess.Popen(                          # argv 数组，不经过 shell
            argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL
        )
        try:
            _wait_port(local_port, proc, timeout=20.0)
            yield local_port
        finally:
            proc.terminate()
            with suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=5)
            if proc.poll() is None:                       # pragma: no cover - 极少走到
                proc.kill()

    def close(self) -> None:
        """立刻拆掉复用连接。

        不调也不会泄漏 —— ``ControlPersist=60s`` 会让它自己过期，复用目录是固定的
        那一个，不会越积越多。
        """
        if os.name == "nt":
            return
        with suppress(Exception):
            subprocess.run(                               # argv 数组，不经过 shell
                [*self._ssh_argv(), "-O", "exit", self.target],
                capture_output=True,
                timeout=10,
            )


# --------------------------------------------------------------------------- #


def _control_dir() -> str:
    """ssh 连接复用的 socket 目录。每用户一个，权限 700。

    带 uid 是为了多人共用一台机器时不撞名 —— 撞了的话另一个人的 socket 就在你的
    ControlPath 上，ssh 会拒绝复用并报一句很难懂的错。
    """
    uid = os.getuid() if hasattr(os, "getuid") else 0
    path = Path(tempfile.gettempdir()) / f"sleight-ssh-{uid}"
    path.mkdir(mode=0o700, exist_ok=True)
    return str(path)


def _decode(raw: bytes | None) -> str:
    return raw.decode("utf-8", errors="replace") if raw else ""


def _tar_gz(root: Path) -> bytes:
    """把目录打成 tar.gz。

    归一化 uid/gid/mtime：同样的内容打出同样的字节，``ext push`` 因此可以靠比对
    校验和判断"要不要重传"。
    """
    if not root.is_dir():
        raise DeployError(f"{root} is not a directory")
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz", format=tarfile.PAX_FORMAT) as tar:
        for path in sorted(root.rglob("*"), key=lambda p: p.as_posix()):
            info = tar.gettarinfo(path, arcname=path.relative_to(root).as_posix())
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            info.mtime = 0
            if info.isreg():
                with path.open("rb") as fh:
                    tar.addfile(info, fh)
            else:
                tar.addfile(info)
            if buf.tell() > MAX_DIR_BYTES:
                raise DeployError(
                    f"{root} is larger than {MAX_DIR_BYTES // 1024 // 1024} MB — "
                    "that is almost certainly the wrong directory"
                )
    return buf.getvalue()


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_port(port: int, proc: subprocess.Popen[bytes], *, timeout: float) -> None:
    """等隧道真的通了再放行。

    ``ExitOnForwardFailure=yes`` 让转发失败时 ssh 直接退出，所以这里同时盯进程 ——
    否则会白等满 timeout 再报一个没有原因的超时。
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            err = _decode(proc.stderr.read() if proc.stderr else b"").strip()
            raise DeployError(f"ssh tunnel failed to start: {err or f'exit {proc.returncode}'}")
        with socket.socket() as sock:
            sock.settimeout(0.5)
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.1)
    raise DeployError(f"ssh tunnel did not come up on 127.0.0.1:{port} within {timeout:.0f}s")
