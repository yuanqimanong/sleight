"""本地启动器：起一个本机浏览器进程，再把它当普通 CDP 端点驱动。

和 :class:`~sleight.providers.plain.Plain` 的区别只有一件事 —— Plain 只能连一个**已经
在跑**的端点，本类**负责把进程拉起来、探活、退出时清理**。这样"用 Sleight 操作本地
指纹浏览器"就不用手动先开一个 `chrome --remote-debugging-port`。

它对浏览器内核**不挑**：标准 Chromium、fingerprint-chromium、Cloak 本地 binary 都行 ——
换的只是 ``binary`` 路径和启动参数，业务代码（Session/Pool/Lease）完全不变。指纹内核
（如 fingerprint-chromium / Cloak）用 ``fingerprint=<seed>`` 给一个稳定人格：同一个 seed
每次启动得到同一套指纹。

    >>> from sleight.providers import LocalLauncher
    >>> with LocalLauncher("fingerprint-chromium", fingerprint=42) as browser:
    ...     with browser.lease() as inst, inst.session() as s:
    ...         s.open("https://example.com")

跨平台：Windows / macOS / Linux 都可用。``binary`` 传可执行文件路径，或 ``PATH`` 上的
名字；macOS 传 ``.app`` 里的真实可执行文件路径。Linux 上以 root 或在容器里跑时通常要
``no_sandbox=True``。
"""

from __future__ import annotations

import atexit
import contextlib
import logging
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from collections.abc import Sequence

from ..core._http import HttpClient
from ..core.errors import ConnectionError, InstanceError, NotFound
from ..core.types import Endpoint, InstanceInfo, InstanceStatus
from .base import BaseProvider

__all__ = ["LocalLauncher"]

DEFAULT_ID = "default"


def _free_port(host: str) -> int:
    with socket.socket() as sock:
        sock.bind((host, 0))
        return sock.getsockname()[1]


def _resolve_binary(binary: str) -> str:
    """把 ``binary`` 解析成一个真实可执行文件路径。

    带路径分隔符或本身存在的当作路径；否则去 ``PATH`` 上找。
    """
    if os.sep in binary or (os.altsep and os.altsep in binary) or os.path.exists(binary):
        if not os.path.exists(binary):
            raise InstanceError(f"browser binary not found: {binary!r}")
        return binary
    found = shutil.which(binary)
    if not found:
        raise InstanceError(f"browser binary {binary!r} not found on PATH")
    return found


log = logging.getLogger("sleight.provider")


class LocalLauncher(BaseProvider):
    """启动并驱动一个**本机**浏览器进程（单实例，id 为 ``"default"``）。

    :param binary: 浏览器可执行文件路径，或 ``PATH`` 上的名字（如 ``chromium`` /
        ``fingerprint-chromium``）
    :param fingerprint: 指纹种子。给了就传 ``--fingerprint=<seed>``（fingerprint-chromium /
        Cloak 支持）—— 同 seed 每次启动同一套指纹。标准 Chromium 不认这个参数，别传
    :param headless: 无头模式（``--headless=new``）。要有头 UI 就传 ``False``
    :param port: CDP 调试端口。``0`` 自动挑一个空闲端口
    :param host: 监听地址，默认只在 loopback（``127.0.0.1``），不要暴露公网
    :param profile_dir: 用户数据目录。``None`` 用一个临时目录，``close()`` 时删掉；给了
        路径就是持久 Profile（登录态/指纹种子跨会话保留），**不**自动删
    :param no_sandbox: 传 ``--no-sandbox``。Linux 上以 root 或容器里跑通常必须开
    :param args: 追加的启动参数（原样拼在后面）
    :param name: 池内唯一的 provider 名，也是 uid 前缀
    :param start_timeout: 等 CDP 端点就绪的总时限，秒
    """

    def __init__(
        self,
        binary: str,
        *,
        fingerprint: int | None = None,
        headless: bool = True,
        port: int = 0,
        host: str = "127.0.0.1",
        profile_dir: str | None = None,
        no_sandbox: bool = False,
        args: Sequence[str] = (),
        name: str = "local",
        start_timeout: float = 30.0,
    ) -> None:
        self.name = name
        self._binary = _resolve_binary(binary)
        self._fingerprint = fingerprint
        self._headless = headless
        self._host = host
        self._port = port
        self._no_sandbox = no_sandbox
        self._extra_args = list(args)
        self._start_timeout = start_timeout

        self._profile_dir = profile_dir
        self._owns_profile = profile_dir is None
        self._proc: subprocess.Popen[bytes] | None = None
        self._http: HttpClient | None = None
        self._lock = threading.Lock()
        self._closed = False
        # 忘了 close 也别把进程/临时目录漏在系统里
        self._finalizer = atexit.register(self.close)

    def __repr__(self) -> str:
        where = f":{self._port}" if self._port else " (not started)"
        return f"<LocalLauncher {self.name!r} {os.path.basename(self._binary)}{where}>"

    def __enter__(self) -> LocalLauncher:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------ #
    # Provider 协议
    # ------------------------------------------------------------------ #

    def list_instances(self) -> list[InstanceInfo]:
        """固定一个实例，id 为 ``"default"``。"""
        return [
            InstanceInfo(
                id=DEFAULT_ID,
                provider=self.name,
                ready=self.status(DEFAULT_ID) is InstanceStatus.RUNNING,
                name=f"{os.path.basename(self._binary)}@{self._host}:{self._port or '?'}",
            )
        ]

    def status(self, instance_id: str) -> InstanceStatus:
        """进程活着且 ``/json/version`` 有响应才算 RUNNING。

        :param instance_id: ``"default"`` 之外一律 NOT_FOUND
        """
        if instance_id not in (DEFAULT_ID, None):
            return InstanceStatus.NOT_FOUND
        if self._proc is None or self._proc.poll() is not None or self._http is None:
            return InstanceStatus.STOPPED
        try:
            return (
                InstanceStatus.RUNNING
                if self._http.get("/json/version").ok
                else InstanceStatus.STOPPED
            )
        except ConnectionError:
            return InstanceStatus.STOPPED

    def ensure_ready(self, instance_id: str) -> None:
        """确保本机进程在跑。**幂等** —— 已在跑直接返回。

        :param instance_id: 只接受 ``"default"``
        :raises NotFound: 传了别的 id
        :raises InstanceError: 进程起不来，或没在 ``start_timeout`` 内暴露出 CDP
        """
        self._check_id(instance_id)
        with self._lock:
            if self._closed:
                raise InstanceError(f"{self.name}: launcher is closed")
            if self.status(DEFAULT_ID) is InstanceStatus.RUNNING:
                return
            self._launch_locked()

    def endpoint(self, instance_id: str | None = None) -> Endpoint:
        """从 ``/json/version`` 取浏览器级 WS 地址。

        :param instance_id: 只接受 ``None`` 或 ``"default"``
        :raises InstanceError: 还没启动（先 :meth:`ensure_ready`，或用 :meth:`lease`）
        :raises ConnectionError: 端点不可达或响应里没有 ``webSocketDebuggerUrl``
        """
        self._check_id(instance_id)
        if self._http is None:
            raise InstanceError(f"{self.name}: not started yet — call ensure_ready() or lease()")
        r = self._http.get("/json/version")
        if not r.ok or not isinstance(r.body, dict):
            raise ConnectionError(f"{self._base_url()}/json/version returned {r.status}")
        ws = r.body.get("webSocketDebuggerUrl")
        if not ws:
            raise ConnectionError(f"{self._base_url()}/json/version has no webSocketDebuggerUrl")
        return Endpoint(http_base=self._base_url(), ws_url=ws, headers={})

    def recover(self, instance_id: str) -> None:
        """进程崩了就重启（复用同一个 Profile / 指纹种子）。

        **只恢复进程，不重放业务动作** —— 重放可能意味着重复提交。
        """
        self._check_id(instance_id)
        with self._lock:
            if self._closed:
                raise InstanceError(f"{self.name}: launcher is closed")
            log.warning("%s: recovering local browser", self.name)
            self._terminate_locked()
            self._launch_locked()

    def close(self) -> None:
        """停进程、清理临时 Profile。幂等，可重复调；也在 ``__exit__`` / 退出时自动触发。"""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._terminate_locked()
            if self._owns_profile and self._profile_dir:
                shutil.rmtree(self._profile_dir, ignore_errors=True)
                self._profile_dir = None
        with contextlib.suppress(Exception):
            atexit.unregister(self._finalizer)

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #

    def _check_id(self, instance_id: str | None) -> None:
        if instance_id not in (None, DEFAULT_ID):
            raise NotFound(f"{self.name}: LocalLauncher has a single instance {DEFAULT_ID!r}")

    def _base_url(self) -> str:
        return f"http://{self._host}:{self._port}"

    def _build_args(self) -> list[str]:
        # remote-debugging 默认只绑 loopback；新版 Chromium 还限制了改绑地址，所以不传
        # --remote-debugging-address，用默认的 127.0.0.1 最稳（也正是这里连接的地址）。
        args = [
            self._binary,
            f"--remote-debugging-port={self._port}",
            f"--user-data-dir={self._profile_dir}",
        ]
        if self._headless:
            args.append("--headless=new")
        if self._no_sandbox:
            args.append("--no-sandbox")
        if self._fingerprint is not None:
            args.append(f"--fingerprint={self._fingerprint}")
        args.extend(self._extra_args)
        args.append("about:blank")
        return args

    def _launch_locked(self) -> None:
        """调用者必须持有 ``self._lock``。"""
        if self._owns_profile:
            self._profile_dir = tempfile.mkdtemp(prefix="sleight-local-")
        if self._port == 0:
            self._port = _free_port(self._host)
        self._http = HttpClient(self._base_url(), timeout=5.0)

        try:
            self._proc = subprocess.Popen(
                self._build_args(),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError as exc:
            raise InstanceError(f"{self.name}: could not start {self._binary!r}: {exc}") from exc

        deadline = time.monotonic() + self._start_timeout
        while time.monotonic() < deadline:
            if self._proc.poll() is not None:
                raise InstanceError(
                    f"{self.name}: browser exited early (rc={self._proc.returncode}) "
                    "— wrong binary or flags?"
                )
            with contextlib.suppress(ConnectionError):
                if self._http.get("/json/version").ok:
                    return
            time.sleep(0.15)
        # 没起来：别把半死不活的进程留着
        self._terminate_locked()
        raise InstanceError(
            f"{self.name}: {os.path.basename(self._binary)} did not expose CDP at "
            f"{self._base_url()} within {self._start_timeout}s"
        )

    def _terminate_locked(self) -> None:
        """调用者必须持有 ``self._lock``。停进程，尽量优雅，兜底强杀。"""
        proc, self._proc = self._proc, None
        self._http = None
        if proc is None or proc.poll() is not None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            with contextlib.suppress(Exception):
                proc.wait(timeout=5)
