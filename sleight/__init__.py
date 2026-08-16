"""sleight —— 通过 CDP 驱动真实浏览器，自带拟人交互与实例池管理。

    >>> from sleight import connect
    >>> with connect("http://127.0.0.1:9222") as s:
    ...     s.open("https://example.com")
    ...     print(s.title())
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _installed_version
from typing import Any

from ._logging import enable_debug_logging
from .core import errors
from .core.element import Element
from .core.errors import (
    AuthError,
    Busy,
    Crashed,
    ElementError,
    InstanceError,
    LeaseLost,
    NotFound,
    NotReady,
    SessionLost,
    SleightError,
    StaleRef,
)
from .core.extract import ExtractedDocument
from .core.human import CAREFUL, DEFAULT, FAST, HumanProfile
from .core.resources import NetworkResource
from .core.session import Selectable, Session
from .core.snapshot import BackendElement, Snapshot, SnapshotNode
from .core.static import StaticElement
from .core.transport import Transport
from .core.types import (
    Box,
    ClearReport,
    Condition,
    DomReady,
    Endpoint,
    Gone,
    InstanceInfo,
    InstanceStatus,
    Load,
    NetworkIdle,
    Point,
    Selector,
    StorageType,
    Text,
)
from .pool import BrowserContext, InstanceHandle, Pool

try:
    __version__ = _installed_version("sleight")
except PackageNotFoundError:      # 从源码目录直接 import，没装进环境
    __version__ = "0.0.0.dev0"

__all__ = [  # noqa: RUF022 - 按语义分组，不按字母序
    "__version__",
    "connect",
    "launch",
    "Pool",
    "InstanceHandle",
    "BrowserContext",
    "Session",
    "Selectable",
    "Element",
    "Snapshot",
    "SnapshotNode",
    "BackendElement",
    "StaticElement",
    "ExtractedDocument",
    "NetworkResource",
    "Transport",
    "enable_debug_logging",
    # 拟人预设
    "HumanProfile",
    "FAST",
    "DEFAULT",
    "CAREFUL",
    # 等待条件
    "DomReady",
    "Load",
    "Text",
    "Selector",
    "Gone",
    "NetworkIdle",
    "Condition",
    # 数据类型
    "Point",
    "Box",
    "Endpoint",
    "InstanceInfo",
    "InstanceStatus",
    "StorageType",
    "ClearReport",
    # 异常
    "errors",
    "SleightError",
    "AuthError",
    "InstanceError",
    "NotFound",
    "NotReady",
    "Busy",
    "Crashed",
    "SessionLost",
    "LeaseLost",
    "ElementError",
    "StaleRef",
]


class _Connection:
    """``connect()`` 的返回：一个 Session，退出时把 transport 也带走。"""

    def __init__(self, transport: Transport, session: Session) -> None:
        self._t = transport
        self.session = session

    def __enter__(self) -> Session:
        return self.session

    def __exit__(self, *exc: object) -> None:
        try:
            self.session.close()
        finally:
            self._t.close()


def connect(url: str, *, headers: dict[str, str] | None = None, **kw: Any) -> _Connection:
    """连一个裸 CDP 端点，**新建自有 tab**。

    ``url`` 可以是 ``http://host:port``（走 ``/json/version`` 发现）或直接的
    ``ws://…`` 浏览器级端点。

    自建 tab 而不是接管既有的 —— target 顺序没有业务语义，接管等于随机修改一个
    别人的页面。
    """
    if url.startswith(("ws://", "wss://")):
        ws_url = url
    else:
        from .providers.plain import Plain

        ep: Endpoint = Plain(url, headers=headers).endpoint()
        ws_url = ep.ws_url
        headers = dict(ep.headers) or headers

    transport = Transport.connect(ws_url, headers=headers)
    try:
        return _Connection(transport, Session.create(transport, **kw))
    except BaseException:
        transport.close()
        raise


class _LaunchedConnection:
    """``launch()`` 的返回：启动的本机浏览器 + 一个 Session，退出时都收干净。"""

    def __init__(self, launcher: Any, transport: Transport, session: Session) -> None:
        self._launcher = launcher
        self._t = transport
        self.session = session

    def __enter__(self) -> Session:
        return self.session

    def __exit__(self, *exc: object) -> None:
        try:
            self.session.close()
        finally:
            try:
                self._t.close()
            finally:
                self._launcher.close()      # 停浏览器进程、清理临时 Profile


def launch(
    binary: str,
    *,
    fingerprint: int | None = None,
    headless: bool = True,
    no_sandbox: bool = False,
    profile_dir: str | None = None,
    args: Any = (),
    **kw: Any,
) -> _LaunchedConnection:
    """启动一个**本机**浏览器 binary 并连上它，**新建自有 tab**；退出时停进程、清临时 Profile。

    ``connect()`` 连的是已经在跑的端点；``launch()`` 负责把进程也拉起来。内核不挑 ——
    标准 Chromium、fingerprint-chromium、Cloak 本地 binary 都行，换的只是 ``binary`` 和
    ``fingerprint`` 种子，业务动作一致。需要 Pool/租约就直接用
    :class:`~sleight.providers.local.LocalLauncher`。

        >>> with launch("fingerprint-chromium", fingerprint=42) as s:
        ...     s.open("https://example.com")

    :param binary: 可执行文件路径或 ``PATH`` 上的名字
    :param fingerprint: 指纹种子（fingerprint-chromium / Cloak 支持），同 seed 同人格
    :param headless: 无头，默认 True
    :param no_sandbox: Linux root/容器里通常要 True
    :param profile_dir: 持久 Profile 目录；``None`` 用临时目录并在退出时删除
    :param args: 追加的启动参数
    :param kw: 透传给 :meth:`Session.create`（``human`` / ``rng`` / ``track_network`` /
        ``track_runtime``）
    """
    from .providers.local import LocalLauncher

    launcher = LocalLauncher(
        binary, fingerprint=fingerprint, headless=headless,
        no_sandbox=no_sandbox, profile_dir=profile_dir, args=args,
    )
    try:
        launcher.ensure_ready("default")
        ep = launcher.endpoint("default")
        transport = Transport.connect(ep.ws_url, headers=dict(ep.headers))
        try:
            return _LaunchedConnection(launcher, transport, Session.create(transport, **kw))
        except BaseException:
            transport.close()
            raise
    except BaseException:
        launcher.close()
        raise
