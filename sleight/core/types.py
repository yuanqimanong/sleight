"""核心数据类型。

约定：
- 所有对外类型 ``frozen=True``。但 **frozen 不等于不可变** —— 冻结只阻止字段重新
  赋值，dict/set 字段的内容照样能改。所有映射字段在 ``__post_init__`` 里复制并包成
  ``MappingProxyType``。
- 携带凭据的字段一律 ``repr=False``（见 :mod:`sleight.core._redact`）。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import ClassVar

__all__ = [
    "Box",
    "ClearReport",
    "Condition",
    "DomReady",
    "Endpoint",
    "Gone",
    "InstanceInfo",
    "InstanceStatus",
    "Load",
    "NetworkIdle",
    "Point",
    "Selector",
    "StorageType",
    "Text",
]


class StorageType(StrEnum):
    """``Storage.clearDataForOrigin`` 认的存储类型。

    做成枚举而不是收裸字符串：CDP 那边是**逗号分隔的字符串**，拼错一个词不报错、
    静默不生效 —— 你以为清干净了，其实什么都没动。
    """

    COOKIES = "cookies"
    LOCAL_STORAGE = "local_storage"
    INDEXEDDB = "indexeddb"
    CACHE_STORAGE = "cache_storage"
    SERVICE_WORKERS = "service_workers"
    WEBSQL = "websql"
    FILE_SYSTEMS = "file_systems"
    SHADER_CACHE = "shader_cache"
    INTEREST_GROUPS = "interest_groups"
    SHARED_STORAGE = "shared_storage"
    STORAGE_BUCKETS = "storage_buckets"
    ALL = "all"


@dataclass(frozen=True, slots=True)
class ClearReport:
    """:meth:`Session.clear_site_data() <sleight.core.session.Session.clear_site_data>`
    清掉了什么。

    存在的理由只有一个：排查时「datadome 这个 cookie 到底清没清掉」比什么都值钱，
    而 CDP 的清理命令**什么都不返回**。
    """

    origin: str
    """实际清理的 origin，已经归一化过（去掉 path / query）。"""

    types: tuple[StorageType, ...]
    """这次清了哪几类。"""

    cookies: tuple[str, ...] | None
    """清掉的 cookie 名字。``None`` 表示**没测量**（Network domain 没开），
    和 ``()``「一个都没有」是两回事。"""

    usage_before: int
    """清理前该 origin 占用的字节数。"""

    usage_after: int
    """清理后。仍然不为 0 不一定是失败 —— 有些类型不在 ``types`` 里。"""

    def __bool__(self) -> bool:
        """有没有真的清掉东西。空清理是最常见的"看起来成功了"。"""
        return bool(self.cookies) or self.usage_after < self.usage_before

@dataclass(frozen=True, slots=True)
class Point:
    """一个坐标点。

    viewport CSS 像素，**整数** —— 真实鼠标不产生小数坐标，拿小数位当"抖动"
    是一眼可辨的机器特征。构造时会自动取整。
    """

    x: int
    y: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "x", int(self.x))
        object.__setattr__(self, "y", int(self.y))


@dataclass(frozen=True, slots=True)
class Box:
    """``getBoundingClientRect()`` 的原值，viewport CSS 像素。

    不乘 devicePixelRatio，不加 scrollX/scrollY —— CDP Input 用的就是这个坐标系。
    """

    x: float
    y: float
    w: float
    h: float

    @property
    def center(self) -> Point:
        return Point(round(self.x + self.w / 2), round(self.y + self.h / 2))

    @property
    def empty(self) -> bool:
        return self.w <= 0 or self.h <= 0


@dataclass(frozen=True, slots=True)
class Endpoint:
    """一个可连的 CDP 端点。

    ``headers`` 不是可选装饰 —— CloakBrowser 实测 WS 握手不带 ``Authorization``
    直接 403，凭据塞不进 URL。
    """

    http_base: str
    ws_url: str = field(repr=False)
    headers: Mapping[str, str] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "headers", MappingProxyType(dict(self.headers)))

    def header_list(self) -> list[str]:
        """把 :attr:`headers` 转成 websocket-client 要的 ``["K: V", ...]`` 形式。"""
        return [f"{k}: {v}" for k, v in self.headers.items()]


class InstanceStatus(StrEnum):
    """Provider.status() 的返回。

    ``NOT_FOUND`` 只能由 status() 给出 —— launch/stop 的状态码分不清它和"已停止"。
    """

    RUNNING = "running"
    STOPPED = "stopped"
    NOT_FOUND = "not_found"


@dataclass(frozen=True, slots=True)
class InstanceInfo:
    id: str
    provider: str
    ready: bool = False
    name: str = ""
    tags: frozenset[str] = frozenset()
    labels: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "tags", frozenset(self.tags))
        object.__setattr__(self, "labels", MappingProxyType(dict(self.labels)))

    @property
    def uid(self) -> str:
        """``"{provider}:{id}"`` —— 全局唯一，也是租约 key 的主体部分。

        多个 ``Plain`` provider 的实例 id 都叫 ``"default"``，不带前缀会直接撞车。
        """
        return f"{self.provider}:{self.id}"


# --------------------------------------------------------------------------- #
# 等待条件
#
# 用类型化对象取代 wait_for + wait_until 两个字符串参数 —— 原来的写法既说不清
# "Sign in" 是文本还是选择器，也说不清两个参数同时给时谁生效。
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Condition:
    """等待条件基类。子类是纯数据，求值在 :meth:`sleight.core.session.Session._check`。

    ``kind`` 用 ``ClassVar`` 而不是字段：字段会参与 dataclass 的参数排序，
    带默认值的基类字段后面跟无默认值的子类字段会直接报错。
    """

    kind: ClassVar[str] = ""

    def __str__(self) -> str:
        value = getattr(self, "value", None)
        return f"{type(self).__name__}({value!r})" if value is not None else type(self).__name__


@dataclass(frozen=True, slots=True)
class DomReady(Condition):
    kind: ClassVar[str] = "domready"


@dataclass(frozen=True, slots=True)
class Load(Condition):
    kind: ClassVar[str] = "load"


@dataclass(frozen=True, slots=True)
class Text(Condition):
    """页面 innerText 里出现该子串。"""

    value: str
    kind: ClassVar[str] = "text"


@dataclass(frozen=True, slots=True)
class Selector(Condition):
    """选择器命中至少一个元素。"""

    value: str
    kind: ClassVar[str] = "selector"


@dataclass(frozen=True, slots=True)
class Gone(Condition):
    """选择器不再命中任何元素。"""

    value: str
    kind: ClassVar[str] = "gone"


@dataclass(frozen=True, slots=True)
class NetworkIdle(Condition):
    """attach 之后发起的请求全部结束，再静默 ``quiet`` 秒。

    注意语义：**不是"页面全部加载完"** —— attach 之前已经在飞的请求 sleight 看不到。

    :param quiet: 在飞集合空掉之后，还要静默多少秒才算数
    """

    quiet: float = 0.5
    kind: ClassVar[str] = "netidle"

