"""Frame 观察层：枚举 frame 树，并对**可达** frame 做作用域内的只读求值。

作用域和边界（有意为之）：

- :func:`Session.frames` 列出本 target 的整棵 frame 树，并标出每个 frame 是不是
  **可达**——即主 session 是否持有它的 JS 执行上下文。同源 / 同进程的子 frame 可达；
  跨源的 OOPIF 会出现在树里但**不可达**（它在另一个渲染进程、另一个 CDP target），
  读它的内容要另开一条 CDP session，属于下一步的能力。
- :class:`FrameView` 只做**读**（``eval`` / ``text`` / ``html`` / ``count``），在 frame
  自己的 ``executionContextId`` 里求值。点 iframe 里的元素是 ``FrameElement`` 的事
  （复用 :class:`~sleight.core.element.ElementLike` 输入链），不在本模块。

这是 README 里那个"唯一的真缺口"（iframe 支持）的第一块，也是 Snapshot/Ref 逐 frame
采集的前置。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .session import Session

__all__ = ["FrameInfo", "FrameView"]


@dataclass(frozen=True)
class FrameInfo:
    """frame 树里的一个节点。

    :param frame_id: CDP frameId
    :param url: 该 frame 当前地址
    :param name: ``<iframe name>`` / ``id``（CDP 给什么就是什么），可能为空
    :param parent_id: 父 frame 的 id；``None`` 表示顶层
    :param is_main: 是不是顶层主 frame
    :param reachable: 主 session 是否持有它的执行上下文。``True`` = 同源/同进程，可直接
        读；``False`` = 跨源 OOPIF，出现在树里但要另开 session 才能读
    """

    frame_id: str
    url: str
    name: str | None
    parent_id: str | None
    is_main: bool
    reachable: bool


class FrameView:
    """一个**可达** frame 的只读视图 —— 在它自己的执行上下文里求值。

    不做写交互：那会经过坐标换算和命中校验，是 ``FrameElement`` 的职责。
    """

    __slots__ = ("_context_id", "_session", "info")

    def __init__(self, session: Session, info: FrameInfo, context_id: int) -> None:
        self._session = session
        self.info = info
        self._context_id = context_id

    def __repr__(self) -> str:
        return f"<FrameView {self.info.name or self.info.url!r} ctx={self._context_id}>"

    def eval(self, expr: str) -> Any:
        """在**本 frame** 的执行上下文里求值，返回 by-value 结果。

        和 :meth:`Session.eval` 一样只能**读** DOM —— 写交互会产生 ``isTrusted=false``
        的假事件。拼用户输入务必用 ``json.dumps`` 转义。

        :param expr: JS 表达式
        :raises ProtocolError: JS 抛异常，或该 frame 的上下文已失效
        """
        return self._session._eval_in_context(self._context_id, expr)

    def text(self) -> str:
        """``document.body.innerText`` —— 该 frame 的可见文本。"""
        return self.eval("document.body ? document.body.innerText : ''") or ""

    def html(self) -> str:
        """该 frame 渲染后的 ``document.documentElement.outerHTML``。"""
        return self.eval("document.documentElement.outerHTML") or ""

    def count(self, selector: str) -> int:
        """该 frame 内匹配 ``selector`` 的元素个数。"""
        return int(self.eval(f"document.querySelectorAll({json.dumps(selector)}).length") or 0)
