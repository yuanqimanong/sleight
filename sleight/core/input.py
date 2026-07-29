"""Input domain 驱动。

**这是整个库唯一允许发输入事件的地方。**

交互只走 ``Input`` domain。用 ``Runtime.evaluate`` 调 ``el.click()`` 产生的是
``isTrusted=false`` 且坐标 (0,0) 的假事件；读 DOM 用 evaluate 是安全的（读不伪造
事件），写交互不行。

轨迹事件走 ``send_no_wait`` + 本地 sleep + 末尾 ``flush``。每点都等响应的话，一次
点击在跨国链路上就是 20–60 次 RTT —— 本地丝滑、远程慢动作。
"""

from __future__ import annotations

import logging
import time
from random import Random
from typing import TYPE_CHECKING

from .element import Element
from .errors import ElementError
from .human import engine
from .human.presets import DEFAULT, HumanProfile
from .types import Box, Point

if TYPE_CHECKING:
    from .session import Session

log = logging.getLogger("sleight.input")

__all__ = ["Aimable", "HumanSwitch", "InputDriver", "resolve_profile"]

#: 能瞄准的东西：选择器 / 已解析的元素 / 裸坐标 / 裸几何
Aimable = str | Element | Point | Box

#: 单句拟人开关的三态。``None`` 继承 Session 默认，``False`` 直通，
#: ``True`` 用 DEFAULT 预设，给 :class:`HumanProfile` 就用它
HumanSwitch = bool | HumanProfile | None

#: 元素滚进视口后，距离视口顶部的目标比例
_SCROLL_TARGET_RATIO = 0.4
#: 滚动重试上限。没有进展会提前退出，这只是兜底
_SCROLL_ATTEMPTS = 8


def resolve_profile(
    human: bool | HumanProfile | None,
    default: bool | HumanProfile,
) -> HumanProfile | None:
    """把单句的 ``human=`` 参数和 Session 默认合成一个最终 profile。

    :param human: 单句参数。``None`` → 继承 Session 默认；``False`` → 直通；
        ``True`` → :data:`~sleight.core.human.presets.DEFAULT`；
        :class:`~sleight.core.human.presets.HumanProfile` → 用它
    :param default: Session 构造时给的 ``human=``，取值同上但不含 ``None``
    :returns: 生效的 profile；``None`` 表示直通（不拟人）
    :raises TypeError: 传了 bool / HumanProfile / None 之外的东西
    """
    value = default if human is None else human
    if value is False:
        return None
    if value is True:
        return DEFAULT
    if isinstance(value, HumanProfile):
        return value
    raise TypeError(
        f"human must be None, a bool, or a HumanProfile; got {type(value).__name__}"
    )


class InputDriver:
    """把拟人事件序列真正发出去，并维护鼠标状态。

    **维护当前鼠标位置**：下一次移动从上一次的落点出发。每次都从 (0,0) 起步是一眼
    可辨的机器特征。

    :param session: 宿主 Session，用来取 transport / sessionId / 视口尺寸
    :param default_human: 本 Session 的默认拟人档位，见 :func:`resolve_profile`
    :param rng: 随机源。传固定 seed 的 :class:`random.Random` 可复现整段交互
    """

    __slots__ = ("_cursor", "_rng", "_session", "default_human")

    def __init__(
        self,
        session: Session,
        *,
        default_human: bool | HumanProfile = False,
        rng: Random | None = None,
    ) -> None:
        self._session = session
        self._rng = rng if rng is not None else Random()
        self._cursor: Point | None = None
        self.default_human = default_human

    # ------------------------------------------------------------------ #
    # 状态
    # ------------------------------------------------------------------ #

    @property
    def cursor(self) -> Point:
        """当前鼠标位置。首次使用时落在视口里的一个随机点，不是 (0, 0)。"""
        if self._cursor is None:
            w, h = self._session.viewport()
            self._cursor = Point(
                self._rng.randint(int(w * 0.2), int(w * 0.8)),
                self._rng.randint(int(h * 0.2), int(h * 0.8)),
            )
        return self._cursor

    def _profile(self, human: bool | HumanProfile | None) -> HumanProfile | None:
        return resolve_profile(human, self.default_human)

    def _run(self, events: list[engine.Event], *, lead_delay: float = 0.0) -> None:
        """发出事件序列。轨迹段不等响应，末尾一次 ``flush`` 兜底。

        ``flush`` 既是顺序屏障，也是唯一能发现中途某条命令报错的地方。

        :param events: ``(method, params, sleep_after)`` 三元组序列
        :param lead_delay: 派发第一个事件**之前**要等的秒数。间隔平时挂在上一个事件的
            ``sleep_after`` 上，但序列开头没有"上一个" —— 中文整段上屏就属于这种情况
        """
        if not events:
            return
        if lead_delay > 0.0:
            time.sleep(lead_delay)
        transport = self._session.transport
        sid = self._session.cdp_session_id
        for method, params, delay in events:
            transport.send_no_wait(method, params, session_id=sid)
            if delay > 0.0:
                time.sleep(delay)
        # 屏障 + 唯一能发现中途某条命令报错的地方
        transport.flush()
        self._session.drain()

    # ------------------------------------------------------------------ #
    # 目标解析
    # ------------------------------------------------------------------ #

    def _pre_press_delay(self, nth: int, profile: HumanProfile | None) -> float:
        """第 ``nth`` 次按下之前要等多久。

        第一下等的是**反应时间**（到位之后、按下之前）；后续几下等的是双击间隔 ——
        两者是不同的分布，而且 ``inter_click`` 必须落在浏览器的双击判定窗口内
        （Chrome 约 500 ms），不能复用 ``dwell``（那是按住的时长）。

        :param nth: 第几次按下，从 1 开始
        :param profile: 已解析的 profile；``None``（直通）一律不等
        :returns: 秒
        """
        if profile is None:
            return 0.0
        if nth == 1:
            return engine.reaction_delay(self._rng, profile)
        return engine.span(profile.inter_click, self._rng)

    def _target_box(
        self, target: Aimable, *, human: HumanProfile | None
    ) -> tuple[Box | Point, Element | None]:
        """把各种目标形态归一成几何 + 可选的元素（用于命中校验）。

        :param target: 选择器 / Element / 裸坐标 / 裸 Box
        :param human: **已解析**的 profile（``None`` = 直通），不能再传原始的三态值
        :returns: ``(几何, 元素或 None)``。裸坐标没有元素可校验，返回 ``None``
        """
        if isinstance(target, (Point, Box)):
            return target, None
        element = self._session.require(target)
        if not element.in_viewport():
            # human 这里已经是解析过的 profile（None = 直通），不能再走 _profile()：
            # 那会把 None 当成"继承 Session 默认"，直通请求就悄悄变成拟人了
            self._scroll_into_view(element, human)
        return element.require_box(), element

    # ------------------------------------------------------------------ #
    # 动作
    # ------------------------------------------------------------------ #

    def hover(self, target: Aimable, *, human: HumanSwitch = None) -> Point:
        """只移动，不按下。

        :param target: 选择器 / :class:`~sleight.core.element.Element` /
            :class:`~sleight.core.types.Point` / :class:`~sleight.core.types.Box`
        :param human: 三态开关，见 :func:`resolve_profile`
        :returns: 光标最终停在哪
        """
        profile = self._profile(human)
        geometry, _ = self._target_box(target, human=profile)
        events, landing = engine.move_events(
            self.cursor, geometry, rng=self._rng, profile=profile,
            viewport=self._session.viewport(),
        )
        self._run(events)
        self._cursor = landing
        return landing

    def click(
        self,
        target: Aimable,
        *,
        human: HumanSwitch = None,
        button: str = "left",
        click_count: int = 1,
    ) -> Point:
        """移动过去 → 命中校验 → 按下 → 抬起。

        元素不在视口里会先滚进来，然后**重新取 box**。

        :param target: 选择器 / Element / Point / Box
        :param human: 三态开关，见 :func:`resolve_profile`
        :param button: ``left`` / ``right`` / ``middle`` / ``back`` / ``forward``
        :param click_count: 连击次数。2 = 双击，会发**两轮**完整的按下抬起
        :returns: 实际点击的坐标
        :raises ElementError: 选择器没命中、元素零尺寸、或落点被别的元素盖住
        """
        profile = self._profile(human)
        geometry, element = self._target_box(target, human=profile)

        events, landing = engine.move_events(
            self.cursor, geometry, rng=self._rng, profile=profile,
            viewport=self._session.viewport(),
        )

        # 命中校验 #1：取到几何之后、动身之前
        if element is not None:
            element.require_hit(landing.x, landing.y, when="before moving there")

        self._run(events)
        self._cursor = landing

        # 命中校验 #2：按下之前。轨迹要跑 300 ms – 2 s，这期间 cookie 横幅、懒加载的
        # sticky 头部、模态框都可能冒出来盖住目标。只查一次等于没查。
        if element is not None:
            element.require_hit(landing.x, landing.y, when="after the pointer arrived")

        # 真实双击是**两次完整的按下抬起**，detail 依次为 1 和 2。把 clickCount=2
        # 直接塞进单次 press/release 的话，页面永远收不到 detail=1 的 click，而且
        # 第一下 mousedown 就带 detail=2 —— 物理上不可能。
        for nth in range(1, max(click_count, 1) + 1):
            self._run(
                engine.press_events(
                    landing, button=button, rng=self._rng, profile=profile, click_count=nth
                )
                + engine.release_events(landing, button=button, click_count=nth),
                lead_delay=self._pre_press_delay(nth, profile),
            )
        return landing

    def type(
        self,
        target: str | Element | None,
        text: str,
        *,
        human: HumanSwitch = None,
        clear: bool = False,
    ) -> None:
        """逐字符输入。

        :param target: 先点它聚焦。``None`` 表示打到当前焦点上，不做任何点击
        :param text: 要输入的文本。非 ASCII 会成段走 ``Input.insertText``
        :param human: 三态开关，见 :func:`resolve_profile`
        :param clear: 先 Ctrl+A 再 Backspace 清空。**破坏性操作**，执行前会强制
            确认焦点真的落在 ``target`` 上
        :raises ElementError: 目标不存在、被遮挡，或 ``clear=True`` 时焦点没落对
        """
        profile = self._profile(human)
        element: Element | None = None

        if target is not None:
            # 先真实点击聚焦 —— 直接发按键事件会打到当前焦点上，那不一定是这个输入框
            element = self._session.require(target)
            self.click(element, human=human)

        if clear:
            # Ctrl+A 再 Backspace 是**破坏性**的：焦点没落对就等于清空了别的输入框。
            # 点击只保证几何命中（require_hit 查的是 elementFromPoint），不保证元素
            # 可聚焦 —— 点一个 <div> 会让焦点留在原处，键盘事件全打给上一个焦点。
            if element is not None:
                element.require_focus(after="the focusing click")
            self._run(engine.chord_events("Ctrl+A", rng=self._rng, profile=profile))
            self._run(engine.chord_events("Backspace", rng=self._rng, profile=profile))

        events, lead = engine.type_events(text, rng=self._rng, profile=profile)
        self._run(events, lead_delay=lead)

    def press(self, chord: str, *, human: HumanSwitch = None) -> None:
        """按一个键或组合键，打到当前焦点上。

        :param chord: ``"Enter"`` / ``"Ctrl+A"`` / ``"ctrl+shift+k"``，``+`` 分隔
        :param human: 三态开关，只影响按住时长
        :raises ValueError: 键名不认识
        """
        profile = self._profile(human)
        self._run(engine.chord_events(chord, rng=self._rng, profile=profile))

    def scroll(self, dy: int, *, human: HumanSwitch = None) -> None:
        """在光标当前位置滚轮。滚的是**光标下面**那个容器。

        :param dy: 距离，px。正数向下
        :param human: 三态开关。拟人模式切成多个小步，直通模式一次滚到位
        """
        profile = self._profile(human)
        self._run(engine.scroll_events(self.cursor, dy, rng=self._rng, profile=profile))

    def scroll_into_view(self, target: str | Element, *, human: HumanSwitch = None) -> None:
        """把元素滚进视口。

        - **默认**：发真实 ``mouseWheel`` 分步滚动（有滚动事件序列，像人）
        - 直通：``DOM.scrollIntoViewIfNeeded``（CDP 命令，瞬时，无滚轮事件）

        不用 JS ``el.scrollIntoView()`` —— 既瞬时又是脚本发起，两头不讨好。

        只滚垂直方向。横向出界或位于嵌套滚动容器里的元素会明确报错，而不是假装成功。

        :param target: 选择器或 Element
        :param human: 三态开关，见 :func:`resolve_profile`
        :raises ElementError: 滚不动（多半是嵌套滚动容器把滚轮吃掉了）
        """
        self._scroll_into_view(self._session.require(target), self._profile(human))

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #

    def _scroll_into_view(self, element: Element, profile: HumanProfile | None) -> None:
        """``profile`` 已解析：None 就是直通，不再回头查 Session 默认。"""
        if profile is None:
            self._scroll_into_view_instant(element)
            return

        previous_top: float | None = None
        for _ in range(_SCROLL_ATTEMPTS):
            # **成功的判据是 in_viewport()，不是"垂直偏移为 0"。** _scroll_offset 只看
            # top/bottom，横向出界的元素会被判成已经到位并直接返回成功。
            if element.in_viewport():
                return
            metrics = element.scroll_metrics()
            top = metrics["top"]
            offset = round(top - metrics["height"] * _SCROLL_TARGET_RATIO)

            # 没挪动就别再重发同样的距离。滚轮打在视口某个固定点上，若那个点下面压着
            # 另一个可滚容器，它会把整轮滚动全吃掉 —— 原来的实现会照此重试 8 次、
            # 发出上百个 wheel 事件、白等十秒，最后还是失败。
            if offset == 0 or (previous_top is not None and abs(top - previous_top) < 1.0):
                break
            previous_top = top
            self._run(engine.scroll_events(self.cursor, offset, rng=self._rng, profile=profile))

        if not element.in_viewport():
            raise ElementError(
                f"{element!r} will not scroll into view — it may sit in a nested scroll "
                "container, or be off-screen horizontally (only vertical scrolling is "
                "implemented). Try human=False to use DOM.scrollIntoViewIfNeeded."
            )

    def _scroll_into_view_instant(self, element: Element) -> None:
        object_id = element.object_id()
        try:
            self._session.call("DOM.scrollIntoViewIfNeeded", {"objectId": object_id})
        finally:
            # 不释放会把节点钉在渲染进程内存里
            self._session.call("Runtime.releaseObject", {"objectId": object_id})
