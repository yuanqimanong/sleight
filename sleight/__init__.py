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
)
from .core.human import CAREFUL, DEFAULT, FAST, HumanProfile
from .core.resources import NetworkResource
from .core.session import Selectable, Session
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
    "Pool",
    "InstanceHandle",
    "BrowserContext",
    "Session",
    "Selectable",
    "Element",
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
