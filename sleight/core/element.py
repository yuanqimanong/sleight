"""Element：几何 + 命中校验。

作用域限制（有意为之）：**只支持主 frame 的普通 DOM**。不支持 iframe、OOPIF，
不穿透 Shadow DOM。需要这些就用 Playwright。

定位方式是 selector + index 而不是 CDP 的 ``nodeId``/``objectId``：省掉一整套节点
生命周期管理，代价是 DOM 变动后同一个 Element 可能指向不同节点。对驱动层够用；
每次取几何都会重新解析，所以拿到的 box 一定是当下的。
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from .errors import ElementError
from .types import Box

if TYPE_CHECKING:
    from .session import Session

__all__ = ["Element"]


class Element:
    __slots__ = ("_session", "index", "selector")

    def __init__(self, session: Session, selector: str, index: int = 0) -> None:
        self._session = session
        self.selector = selector
        self.index = index

    def __repr__(self) -> str:
        at = f"[{self.index}]" if self.index else ""
        return f"<Element {self.selector!r}{at}>"

    # ------------------------------------------------------------------ #
    # JS 侧的元素引用
    # ------------------------------------------------------------------ #

    @property
    def js_ref(self) -> str:
        """解析到该元素的 JS 表达式。

        用 ``json.dumps`` 而不是 Python 的 ``repr`` —— repr 走 Python 的引号与转义
        规则，遇到反斜杠、引号、非 ASCII 会产出不合法或语义不同的 JS 字面量。
        ``json.dumps`` 产出的恰好是合法 JS 字符串字面量。
        """
        return f"document.querySelectorAll({json.dumps(self.selector)})[{self.index}]"

    def _eval(self, body: str) -> Any:
        """在 ``el`` 绑定到本元素的作用域里求值。元素不存在时 body 不执行。"""
        return self._session.eval(
            f"(() => {{ const el = {self.js_ref}; if (!el) return null; {body} }})()"
        )

    # ------------------------------------------------------------------ #
    # 查询
    # ------------------------------------------------------------------ #

    def exists(self) -> bool:
        """选择器现在还能命中这个元素吗？"""
        return bool(self._eval("return true;"))

    def box(self) -> Box | None:
        """``getBoundingClientRect()`` 的原值，viewport CSS 像素。

        不乘 devicePixelRatio，不加 scroll 偏移 —— CDP Input 用的就是这个坐标系。

        :returns: 元素几何；元素已经不在了返回 ``None``
        """
        raw = self._eval(
            "const r = el.getBoundingClientRect();"
            " return {x: r.x, y: r.y, w: r.width, h: r.height};"
        )
        return Box(**raw) if raw else None

    def require_box(self) -> Box:
        """同 :meth:`box`，但拿不到就抛。

        :raises ElementError: 元素不存在，或宽高为 0（多半是被 CSS 隐藏了）
        """
        box = self.box()
        if box is None:
            raise ElementError(f"no element matches {self.selector!r}[{self.index}]")
        if box.empty:
            raise ElementError(f"{self!r} has zero size ({box.w}x{box.h}); is it hidden?")
        return box

    def text(self) -> str:
        """元素的 ``innerText``。元素不在了返回空串。"""
        return self._eval("return el.innerText;") or ""

    def attr(self, name: str) -> str | None:
        """读一个 HTML 属性。

        :param name: 属性名，会经 ``json.dumps`` 转义后拼进 JS
        :returns: 属性值；属性不存在或元素不在了返回 ``None``
        """
        return self._eval(f"return el.getAttribute({json.dumps(name)});")

    def object_id(self) -> str:
        """拿一个 CDP ``Runtime.RemoteObject`` 句柄，给 ``DOM.*`` 命令用。

        **调用方负责 ``Runtime.releaseObject``** —— 不释放会把节点钉在内存里。
        平时不需要它：读几何走 ``Runtime.evaluate`` 更简单，也不用维护 nodeId 生命周期。

        :returns: 可传给 ``DOM.*`` 命令的 ``objectId``
        :raises ElementError: 元素不存在
        """
        result = self._session.call(
            "Runtime.evaluate", {"expression": self.js_ref, "returnByValue": False}
        )
        remote = result.get("result") or {}
        if not (object_id := remote.get("objectId")):
            raise ElementError(f"no element matches {self.selector!r}[{self.index}]")
        return object_id

    def in_viewport(self) -> bool:
        """元素当前是否有一部分落在视口内，且宽高都不为 0。

        滚动是否到位的判据就是它 —— 不能用"垂直偏移为 0"，那对横向出界的元素
        会误判成成功。
        """
        return bool(
            self._eval(
                "const r = el.getBoundingClientRect();"
                " return r.bottom > 0 && r.right > 0"
                " && r.top < innerHeight && r.left < innerWidth"
                " && r.width > 0 && r.height > 0;"
            )
        )

    def scroll_metrics(self) -> dict[str, float]:
        """``{top, bottom, height}``，供 :class:`~sleight.core.input.InputDriver`
        计算还要滚多远。``height`` 是视口高度，不是元素高度。

        :raises ElementError: 元素不存在
        """
        raw = self._eval(
            "const r = el.getBoundingClientRect();"
            " return {top: r.top, bottom: r.bottom, height: innerHeight};"
        )
        if raw is None:
            raise ElementError(f"{self!r} no longer exists")
        return raw

    # ------------------------------------------------------------------ #
    # 命中校验 —— 每次点击前后各做一次
    # ------------------------------------------------------------------ #

    def hit_test(self, x: int, y: int) -> bool:
        """(x, y) 处最上层的元素是不是本元素或其后代。

        false 说明被遮挡（cookie 弹窗、fixed 头部、遮罩层）。不做这步的症状是
        "点了但没反应"，排查极费时间。

        :param x: viewport CSS 像素
        :param y: viewport CSS 像素
        """
        return bool(
            self._eval(
                f"const hit = document.elementFromPoint({x}, {y});"
                " return !!hit && (hit === el || el.contains(hit));"
            )
        )

    def has_focus(self) -> bool:
        """本元素或其后代是不是 ``document.activeElement``。"""
        return bool(
            self._eval(
                "return document.activeElement === el"
                " || el.contains(document.activeElement);"
            )
        )

    def require_focus(self, *, after: str) -> None:
        """确认焦点真的落在本元素上。

        点击只保证**几何命中**（``elementFromPoint``），不保证元素可聚焦：点一个
        ``<div>`` 会让焦点留在原处，随后的键盘事件全打给上一个焦点元素。这在
        ``type(clear=True)`` 下尤其危险 —— Ctrl+A + Backspace 会清空**别的**输入框。

        :param after: 出现在报错消息里的一句话，说明是在哪一步之后检查的，
            例如 ``"the focusing click"``
        :raises ElementError: 焦点不在本元素上；消息里会带上真正的 activeElement
        """
        if self.has_focus():
            return
        active = self._session.eval(
            "(() => { const a = document.activeElement;"
            " return a ? a.tagName.toLowerCase() + (a.id ? '#' + a.id : '') : null; })()"
        )
        raise ElementError(
            f"{self!r} does not have focus {after}"
            f"{f'; document.activeElement is <{active}>' if active else ''}"
            " — is it focusable?"
        )

    def require_hit(self, x: int, y: int, *, when: str) -> None:
        """同 :meth:`hit_test`，但没命中就抛，并在消息里点名挡住它的元素。

        :param x: 要探的 viewport 坐标
        :param y: 要探的 viewport 坐标
        :param when: 出现在报错消息里的时机描述，例如 ``"before moving there"``
        :raises ElementError: 该点最上层的不是本元素
        """
        if not self.hit_test(x, y):
            blocker = self._session.eval(
                f"(() => {{ const h = document.elementFromPoint({x}, {y});"
                " return h ? (h.tagName.toLowerCase()"
                " + (h.id ? '#' + h.id : '')"
                " + (h.className && typeof h.className === 'string'"
                "    ? '.' + h.className.trim().split(/\\s+/).join('.') : '')) : null; })()"
            )
            raise ElementError(
                f"{self!r} is covered at ({x}, {y}) {when}"
                f"{f'; topmost element is <{blocker}>' if blocker else ''}"
            )
