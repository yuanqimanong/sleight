"""核心数据类型。

约定：
- 所有对外类型 ``frozen=True``。但 **frozen 不等于不可变** —— 冻结只阻止字段重新
  赋值，dict/set 字段的内容照样能改。所有映射字段在 ``__post_init__`` 里复制并包成
  ``MappingProxyType``。
- 携带凭据的字段一律 ``repr=False``（见 :mod:`sleight.core._redact`）。
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Any, ClassVar, Literal

__all__ = [
    "Box",
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
    "Text",
]

_EMPTY_MAP: Mapping[str, str] = MappingProxyType({})


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


def replace(obj: Any, **changes: Any) -> Any:
    """``dataclasses.replace`` 的转发。

    :param obj: 任意 dataclass 实例
    :param changes: 字段名 → 新值
    :returns: 改了这些字段的新实例，原对象不变
    """
    return dataclasses.replace(obj, **changes)


HumanMode = bool | None | Literal["default"]
