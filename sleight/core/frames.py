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

from .errors import ElementError
from .types import Box

if TYPE_CHECKING:
    from .element import Element
    from .session import Session

__all__ = ["FrameElement", "FrameInfo", "FrameView"]


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


class FrameElement:
    """同源 iframe 内某个元素的 :class:`~sleight.core.element.ElementLike` 实现。

    几何换算到**顶层 viewport**，输入仍从顶层 session 发（``Input.dispatchMouseEvent``
    只认顶层坐标）；命中校验做**两级**：先在父页确认那一点命中的是这个 iframe，再进
    frame 内确认命中的是目标元素。这样才不会像裸坐标那样"点了没反应"还不报错。

    边界（有意为之、显式失败而非静默误点）：

    - 只支持**单层同源** iframe。跨源 OOPIF 由 :meth:`Session.frame_element` 在构造前
      就挡掉。
    - iframe 或其任一祖先带 CSS ``transform`` 时，"父页坐标 + frame 内坐标"这套线性相加
      在旋转/缩放下会算错落点 —— 这里检测到 transform 直接抛，绝不硬点。
    - frame 内滚动只在直通路径（``human=False`` → ``DOM.scrollIntoViewIfNeeded``）下可靠；
      拟人滚动打的是顶层滚轮，不保证滚动的是 frame，因此元素不在 frame 视口内时命中校验
      会失败并报错，而不是点错地方。
    """

    __slots__ = ("_context_id", "_iframe", "_session", "index", "selector")

    def __init__(
        self, session: Session, iframe: Element, context_id: int,
        selector: str, index: int = 0,
    ) -> None:
        self._session = session
        self._iframe = iframe            # 父页里的 <iframe> Element
        self._context_id = context_id    # iframe 内文档的默认世界执行上下文
        self.selector = selector
        self.index = index

    def __repr__(self) -> str:
        at = f"[{self.index}]" if self.index else ""
        return f"<FrameElement {self.selector!r}{at} in {self._iframe!r}>"

    # —— frame 内求值（绑定 el 到目标元素）——

    @property
    def _js_ref(self) -> str:
        return f"document.querySelectorAll({json.dumps(self.selector)})[{self.index}]"

    def _inner(self, body: str) -> Any:
        return self._session._eval_in_context(
            self._context_id,
            f"(() => {{ const el = {self._js_ref}; if (!el) return null; {body} }})()",
        )

    def _frame_offset(self) -> dict[str, Any] | None:
        """iframe 内容区左上角在**父页 viewport** 里的坐标，并报告有没有 transform。

        在父页上下文里对 ``<iframe>`` 求值。``None`` 表示 iframe 本身已经不在了。
        """
        return self._session.eval(
            f"(() => {{ const f = {self._iframe.js_ref}; if (!f) return null;"
            " const r = f.getBoundingClientRect(); const cs = getComputedStyle(f);"
            " let transformed = false;"
            " for (let n = f; n; n = n.parentElement) {"
            "   if (getComputedStyle(n).transform !== 'none') { transformed = true; break; } }"
            " return {x: r.left + (parseFloat(cs.borderLeftWidth) || 0)"
            "            + (parseFloat(cs.paddingLeft) || 0),"
            "         y: r.top + (parseFloat(cs.borderTopWidth) || 0)"
            "            + (parseFloat(cs.paddingTop) || 0),"
            "         transformed}; })()"
        )

    def _require_offset(self) -> dict[str, Any]:
        off = self._frame_offset()
        if off is None:
            raise ElementError(f"the containing iframe {self._iframe!r} is gone")
        if off.get("transformed"):
            raise ElementError(
                f"{self._iframe!r} (or an ancestor) has a CSS transform; cross-frame "
                "coordinates would be wrong. Not clicking blindly."
            )
        return off

    # —— ElementLike 协议 ——

    def exists(self) -> bool:
        return bool(self._inner("return true;"))

    def require_box(self) -> Box:
        off = self._require_offset()
        inner = self._inner(
            "const r = el.getBoundingClientRect();"
            " return {x: r.x, y: r.y, w: r.width, h: r.height};"
        )
        if inner is None:
            raise ElementError(f"{self!r} is gone")
        return Box(off["x"] + inner["x"], off["y"] + inner["y"], inner["w"], inner["h"])

    def in_viewport(self) -> bool:
        off = self._frame_offset()
        if off is None or off.get("transformed"):
            return False
        inner = self._inner(
            "const r = el.getBoundingClientRect();"
            " const vw = innerWidth, vh = innerHeight;"
            " return {ok: r.bottom > 0 && r.right > 0 && r.top < vh && r.left < vw,"
            "         x: r.x, y: r.y, w: r.width, h: r.height};"
        )
        if not inner or not inner["ok"]:
            return False                      # 先得在 frame 自己的视口里
        vw, vh = self._session.viewport()
        top_x, top_y = off["x"] + inner["x"], off["y"] + inner["y"]
        return top_x < vw and top_y < vh and top_x + inner["w"] > 0 and top_y + inner["h"] > 0

    def scroll_metrics(self) -> dict[str, float]:
        # frame 内坐标即可 —— 直通路径用 object_id() 走 scrollIntoViewIfNeeded，拟人路径
        # 靠 in_viewport() 判定成功，不依赖这里换算到顶层
        m = self._inner(
            "const r = el.getBoundingClientRect();"
            " return {top: r.top, bottom: r.bottom, height: innerHeight};"
        )
        if m is None:
            raise ElementError(f"{self!r} is gone")
        return {"top": float(m["top"]), "bottom": float(m["bottom"]), "height": float(m["height"])}

    def require_hit(self, x: int, y: int, *, when: str) -> None:
        off = self._require_offset()
        # 第一级：父页里这一点命中的必须是这个 iframe
        on_iframe = self._session.eval(
            f"(() => {{ const f = {self._iframe.js_ref};"
            f" const h = document.elementFromPoint({x}, {y});"
            " return !!f && !!h && (h === f || f.contains(h)); })()"
        )
        if not on_iframe:
            raise ElementError(
                f"{self!r}: point ({x}, {y}) is not over its iframe {when} "
                "(something in the parent page is covering it)"
            )
        # 第二级：换算到 frame 内坐标，命中的必须是目标元素或其后代
        fx, fy = round(x - off["x"]), round(y - off["y"])
        hit = self._inner(
            f"const h = el.getRootNode().elementFromPoint({fx}, {fy});"
            " return !!h && (h === el || el.contains(h));"
        )
        if not hit:
            raise ElementError(
                f"{self!r} is covered inside its iframe at frame-local ({fx}, {fy}) {when}"
            )

    def require_focus(self, *, after: str) -> None:
        focused = self._inner(
            "const a = document.activeElement;"
            " return a === el || el.contains(a);"
        )
        if not focused:
            raise ElementError(f"{self!r} does not have focus {after} — is it focusable?")

    def object_id(self) -> str:
        r = self._session.call("Runtime.evaluate", {
            "expression": self._js_ref, "contextId": self._context_id, "returnByValue": False,
        })
        object_id = (r.get("result") or {}).get("objectId")
        if not object_id:
            raise ElementError(f"{self!r} could not be resolved to a live node")
        return object_id
