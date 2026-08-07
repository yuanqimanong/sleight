"""把轨迹和节奏编排成 CDP 事件序列。

**这一层仍然不发送任何东西。** 产物是 ``(method, params, sleep_after)`` 三元组，
谁来发、发到哪，它一概不知 —— 于是算法可以不开浏览器单测，也可以整套喂给
Playwright 的 CDPSession。

所有 CDP 事件的形状集中在这个文件里，直通模式和拟人模式共用，免得两条路径的参数
慢慢长歪。
"""

from __future__ import annotations

from random import Random
from typing import Any, NamedTuple

from ..keymap import (
    CHAR_MAP,
    MOD_CTRL,
    MOD_META,
    MODIFIER_KEYS,
    KeyDef,
    modifier_keydefs,
    parse_chord,
)
from ..types import Box, Point
from .curves import mouse_path
from .presets import HumanProfile
from .timing import key_dwell, scroll_steps, span, type_delays

__all__ = [
    "BUTTONS",
    "Event",
    "chord_events",
    "click_events",
    "drag_events",
    "drop_events",
    "insert_text_event",
    "key_events",
    "modifier_events",
    "move_events",
    "press_events",
    "reaction_delay",
    "release_events",
    "scroll_events",
    "settle_delay",
    "span",
    "type_events",
    "with_trailing_delay",
]

#: ``Input.dispatchMouseEvent`` 的 buttons 位掩码
BUTTONS = {"left": 1, "middle": 4, "right": 2, "back": 8, "forward": 16}


class Event(NamedTuple):
    """一条待发的 CDP 命令，外加发完之后要睡多久。"""

    method: str
    params: dict[str, Any]
    sleep_after: float = 0.0


# --------------------------------------------------------------------------- #
# 鼠标
# --------------------------------------------------------------------------- #


def move_events(
    start: Point,
    target: Box | Point,
    *,
    rng: Random,
    profile: HumanProfile | None,
    held: str | None = None,
    viewport: tuple[int, int] | None = None,
) -> tuple[list[Event], Point]:
    """移动到目标。返回 ``(事件序列, 落点)``。

    ``profile is None`` 是直通模式：一条 ``mouseMoved`` 到中心点。CDP 输入本身是
    ``isTrusted=true`` 的真事件，但浏览器**不会替你补轨迹** —— 直通模式下 press 与
    release 之间就是 0 条 move，这正是本库存在的理由。

    :param start: 光标当前位置
    :param target: 目标。:class:`~sleight.core.types.Box` 会采一个落点，
        :class:`~sleight.core.types.Point` 则精确到该点
    :param rng: 随机源
    :param profile: 拟人参数；``None`` = 直通（一步到位，无轨迹）
    :param held: 移动过程中按住的鼠标键名。``buttons`` 位掩码要如实反映按下状态 ——
        普通移动是 0，拖拽中是对应掩码
    :param viewport: ``(宽, 高)`` CSS 像素，用于把 overshoot 夹在窗口内
    :returns: ``(事件序列, 落点)``
    """
    # 拖拽中的移动**两个字段都要给**。``buttons`` 位掩码页面 JS 读得到，但浏览器自己
    # 的拖拽控制器认的是 ``button`` —— 只给 buttons 的话 JS 实现的滑块照常工作，而
    # HTML5 原生拖放**一声不吭地不启动**：dragstart 不触发，Input.dragIntercepted
    # 也永远不来。真机上验过：同一段轨迹，加上 button='left' 才有 dragstart。
    extra: dict[str, Any] = {"buttons": BUTTONS.get(held or "", 0)}
    if held is not None:
        extra["button"] = held
        if profile is not None:
            # 拖拽走单列的过冲阈值。指针移动那档是 500 px，而滑块总共才一两百像素宽 ——
            # 套过去等于永不过冲，正好丢掉滑块场景里最像人的那个特征
            profile = profile.replace(overshoot_threshold=profile.drag_overshoot_threshold)

    if profile is None:
        landing = target.center if isinstance(target, Box) else target
        return (
            [Event("Input.dispatchMouseEvent",
                   {"type": "mouseMoved", "x": landing.x, "y": landing.y, **extra})],
            landing,
        )

    path = mouse_path(start, target, rng=rng, profile=profile, viewport=viewport)
    events = [
        Event(
            "Input.dispatchMouseEvent",
            {"type": "mouseMoved", "x": p.x, "y": p.y, **extra},
            span(profile.step_delay, rng),
        )
        for p in path
    ]
    return events, path[-1]


def reaction_delay(rng: Random, profile: HumanProfile | None) -> float:
    """到位之后、按下之前的反应时间。

    它是一段**纯等待**，不是一个事件。早先的实现在这里补发一条落点坐标的
    ``mouseMoved`` 来挂这个 sleep —— 但 :func:`~sleight.core.human.curves.mouse_path`
    刚刚才把连续重复点去掉（真实鼠标不上报没动过的位置），紧接着又放回去一个零位移的
    重复 move，而且**每次点击都有、位置永远正好等于落点**。一行
    ``prev.x === cur.x && prev.y === cur.y`` 就能把它挑出来。

    :param rng: 随机源
    :param profile: 取 ``reaction`` 区间；``None``（直通）返回 0
    :returns: 秒。由调用方在派发 mousePressed 之前 sleep 掉
    """
    return span(profile.reaction, rng) if profile is not None else 0.0


def settle_delay(rng: Random, profile: HumanProfile | None) -> float:
    """拖到位之后、松手之前的迟滞。

    和 :func:`reaction_delay` 一样是一段**纯等待**，不是事件 —— 理由也一样：补一条
    零位移的 ``mouseMoved`` 来挂这个 sleep，等于在每次拖拽的终点放一个位置恒等于落点
    的重复上报，一行比较就能挑出来。

    :param rng: 随机源
    :param profile: 取 ``drag_settle`` 区间；``None``（直通）返回 0
    :returns: 秒。由调用方在派发 mouseReleased 之前 sleep 掉
    """
    return span(profile.drag_settle, rng) if profile is not None else 0.0


def with_trailing_delay(events: list[Event], delay: float) -> list[Event]:
    """把一段等待挂到最后一个事件的尾巴上，而不是补一个空事件来承载它。

    :param events: 事件序列。空列表原样返回（没有尾巴可挂）
    :param delay: 秒。``<= 0`` 时原样返回
    :returns: 新列表；``events`` 不变
    """
    if not events or delay <= 0:
        return events
    last = events[-1]
    return [*events[:-1], Event(last.method, last.params, last.sleep_after + delay)]


def press_events(
    at: Point, *, button: str, rng: Random, profile: HumanProfile | None, click_count: int = 1
) -> list[Event]:
    """按下。

    :param at: 按下的坐标
    :param button: ``left`` / ``right`` / ``middle`` / ``back`` / ``forward``
    :param rng: 随机源
    :param profile: 取 ``dwell``（按住时长）挂在事件尾部；``None``（直通）为 0
    :param click_count: 第几连击。**必填字段** —— 缺了部分站点的 handler 不触发
    :returns: 单元素事件列表
    :raises ValueError: ``button`` 不认识
    """
    mask = BUTTONS.get(button)
    if mask is None:
        raise ValueError(f"unknown mouse button {button!r}; expected one of {sorted(BUTTONS)}")
    return [
        Event(
            "Input.dispatchMouseEvent",
            {"type": "mousePressed", "x": at.x, "y": at.y, "button": button,
             "buttons": mask, "clickCount": click_count},
            span(profile.dwell, rng) if profile is not None else 0.0,
        )
    ]


def release_events(at: Point, *, button: str, click_count: int = 1) -> list[Event]:
    """抬起。``buttons`` 位掩码回到 0，``clickCount`` 要与对应的按下一致。

    :param at: 抬起的坐标，与按下同一个点
    :param button: 与按下同一个键
    :param click_count: 与对应的 :func:`press_events` 一致
    """
    return [
        Event(
            "Input.dispatchMouseEvent",
            {"type": "mouseReleased", "x": at.x, "y": at.y, "button": button,
             "buttons": 0, "clickCount": click_count},
        )
    ]


def click_events(
    start: Point,
    target: Box | Point,
    *,
    rng: Random,
    profile: HumanProfile | None,
    button: str = "left",
    click_count: int = 1,
) -> tuple[list[Event], Point]:
    """完整的一次点击。

    ⚠️ 只在没有命中校验需求时用它。:mod:`sleight.core.input` 走的是拆开的
    ``move_events`` → 命中校验 → ``press_events`` → ``release_events``，因为轨迹要跑
    300 ms 到 2 s，这期间 cookie 横幅、懒加载的 sticky 头部、模态框都可能冒出来盖住
    目标，按下之前必须**再查一次**。

    :param start: 光标当前位置
    :param target: 目标 Box 或 Point
    :param rng: 随机源
    :param profile: 拟人参数；``None`` = 直通
    :param button: 鼠标键名
    :param click_count: 连击计数
    :returns: ``(事件序列, 落点)``
    """
    moves, landing = move_events(start, target, rng=rng, profile=profile)
    # 反应时间挂在最后一个轨迹点的尾巴上，不补发零位移的 move
    moves = with_trailing_delay(moves, reaction_delay(rng, profile))
    return (
        moves
        + press_events(landing, button=button, rng=rng, profile=profile, click_count=click_count)
        + release_events(landing, button=button, click_count=click_count),
        landing,
    )


def drag_events(
    start: Point,
    source: Box | Point,
    target: Box | Point,
    *,
    rng: Random,
    profile: HumanProfile | None,
    button: str = "left",
    viewport: tuple[int, int] | None = None,
) -> tuple[list[Event], Point]:
    """完整的一次鼠标拖拽：移动 → 按下 → 按住移动 → 迟滞 → 抬起。

    和"点一下再移动"的关键差别有三处，缺一处就不成立：

    * 拖拽段的 ``buttons`` 位掩码必须一路非零。掩码回 0 的移动是 hover，页面的
      ``mousemove`` handler 一看 ``e.buttons`` 就知道键没按着；
    * 过冲阈值走 ``drag_overshoot_threshold``（见 :func:`move_events`）；
    * 松手前有一段 :func:`settle_delay`。

    ⚠️ 和 :func:`click_events` 一样，:mod:`sleight.core.input` 不走这个函数 —— 它要在
    按下之前插命中校验，还要探 HTML5 原生拖放。这里是给不需要那些的调用方用的。

    :param start: 光标当前位置
    :param source: 从哪抓起。Box 会采点，Point 精确到该点
    :param target: 拖到哪，同上
    :param rng: 随机源
    :param profile: 拟人参数；``None`` = 直通（两段各一条 move）
    :param button: 按住哪个键拖
    :param viewport: ``(宽, 高)`` CSS 像素
    :returns: ``(事件序列, 松手点)``
    :raises ValueError: ``button`` 不认识
    """
    approach, grab_at = move_events(start, source, rng=rng, profile=profile, viewport=viewport)
    approach = with_trailing_delay(approach, reaction_delay(rng, profile))
    haul, release_at = move_events(
        grab_at, target, rng=rng, profile=profile, held=button, viewport=viewport
    )
    haul = with_trailing_delay(haul, settle_delay(rng, profile))
    return (
        approach
        + press_events(grab_at, button=button, rng=rng, profile=profile)
        + haul
        + release_events(release_at, button=button),
        release_at,
    )


def drop_events(moves: list[Event], data: dict[str, Any]) -> list[Event]:
    """把一段 ``mouseMoved`` 轨迹翻成 HTML5 原生拖放事件。

    ``Input.setInterceptDrags`` 打开之后，浏览器把原生拖放交给我们驱动 —— 这时候页面
    收到的应该是 ``dragover`` 而**不是** ``mousemove``。所以这里是"翻译"而不是"追加"：
    同一条轨迹，换一套事件名，时序原样保留。

    :param moves: :func:`move_events` 产出的轨迹段，**不能为空**
    :param data: ``Input.dragIntercepted`` 给回来的 ``data``，每条事件都要原样带上
    :returns: ``dragEnter`` → ``dragOver``×n → ``drop``
    :raises ValueError: ``moves`` 为空
    """
    if not moves:
        raise ValueError("drop_events() needs at least one move to translate")
    out = [
        # 第一条是 dragEnter —— 少了它，靠 dragenter 点亮放置区的实现收不到任何信号
        Event(
            "Input.dispatchDragEvent",
            {"type": "dragEnter" if i == 0 else "dragOver",
             "x": ev.params["x"], "y": ev.params["y"], "data": data},
            ev.sleep_after,
        )
        for i, ev in enumerate(moves)
    ]
    last = moves[-1]
    out.append(Event(
        "Input.dispatchDragEvent",
        {"type": "drop", "x": last.params["x"], "y": last.params["y"], "data": data},
    ))
    return out


def scroll_events(
    at: Point, dy: int, *, rng: Random, profile: HumanProfile | None
) -> list[Event]:
    """滚轮。拟人模式切成多个小步，真实滚轮一格约 100 px。

    :param at: 滚轮事件的坐标 —— 决定滚的是**光标下面**的哪个容器
    :param dy: 总距离，px。正数向下，0 返回空列表
    :param rng: 随机源
    :param profile: 取 ``scroll_step`` / ``scroll_delay``；``None`` = 一次滚到位
    :returns: 事件列表
    """
    if dy == 0:
        return []
    if profile is None:
        return [Event("Input.dispatchMouseEvent",
                      {"type": "mouseWheel", "x": at.x, "y": at.y, "deltaX": 0, "deltaY": dy})]
    return [
        Event(
            "Input.dispatchMouseEvent",
            {"type": "mouseWheel", "x": at.x, "y": at.y, "deltaX": 0, "deltaY": step},
            span(profile.scroll_delay, rng),
        )
        for step in scroll_steps(dy, rng=rng, profile=profile)
    ]


# --------------------------------------------------------------------------- #
# 键盘
# --------------------------------------------------------------------------- #


def _key_event(
    kind: str, keydef: KeyDef, *, modifiers: int, with_text: bool
) -> Event:
    params: dict[str, Any] = {
        "type": kind,
        "key": keydef.key,
        "code": keydef.code,
        "windowsVirtualKeyCode": keydef.keycode,
    }
    if modifiers:
        params["modifiers"] = modifiers
    if with_text and keydef.text and kind == "keyDown":
        # 可打印字符必须给 text，否则只有 keydown 事件、输入框里什么都没有。
        # unmodifiedText 是不按 Shift 时该键产生的字符。
        params["text"] = keydef.text
        params["unmodifiedText"] = keydef.text.lower() if keydef.shift else keydef.text
    return Event("Input.dispatchKeyEvent", params)


def key_events(
    keydef: KeyDef,
    *,
    modifiers: int = 0,
    dwell: float = 0.0,
    with_text: bool = True,
) -> list[Event]:
    """一次完整的按键：keyDown（带 text）→ 停留 → keyUp。

    **不含修饰键本身的 keydown/keyup** —— 那由 :func:`modifier_events` 按"按住一段"
    的方式包在外面，见 :func:`type_events`。

    ``Ctrl`` 组合键不带 text —— 带了会在插入 ``a`` 的同时触发全选。

    :param keydef: 键位定义，来自 :data:`~sleight.core.keymap.CHAR_MAP`
        或 :func:`~sleight.core.keymap.parse_chord`
    :param modifiers: 位掩码 Alt=1 Ctrl=2 Meta=4 Shift=8
    :param dwell: keyDown 之后按住多久，秒
    :param with_text: 是否附带 ``text`` / ``unmodifiedText``。可打印字符必须为 True，
        否则页面有 keydown 事件但输入框里什么都没有
    :returns: ``[keyDown, keyUp]``
    """
    emit_text = with_text and not (modifiers & (MOD_CTRL | MOD_META))
    down = _key_event("keyDown", keydef, modifiers=modifiers, with_text=emit_text)
    return [
        Event(down.method, down.params, dwell),
        _key_event("keyUp", keydef, modifiers=modifiers, with_text=False),
    ]


def modifier_events(mask: int, *, pressed: bool, active: int) -> list[Event]:
    """修饰键自己的 keyDown / keyUp。

    只设 ``modifiers`` 位掩码是不够的：页面会看到 ``shiftKey=true`` 的 keydown，
    却**从来没有** ``key === 'Shift'`` 的事件。真键盘不可能产生这种组合。

    :param mask: 这次要按下/抬起哪些修饰键，位掩码
    :param pressed: True 发 keyDown（按顺序），False 发 keyUp（逆序）
    :param active: 事件发出时**已经按住**的修饰键掩码，要如实累加进 ``modifiers``
    :returns: 修饰键自己的按键事件列表
    """
    out: list[Event] = []
    keydefs = modifier_keydefs(mask)
    state = active
    for keydef in keydefs if pressed else list(reversed(keydefs)):
        bit = next(b for b, kd in MODIFIER_KEYS if kd.code == keydef.code)
        # 状态要**逐个累加**：按下 Shift 时 Ctrl 已经按住了，modifiers 必须是两位都有。
        # 每次都从 active 重算的话，Ctrl+Shift 里的 Shift 会报成只有 Shift。
        state = state | bit if pressed else state & ~bit
        out.append(_key_event("keyDown" if pressed else "keyUp", keydef,
                              modifiers=state, with_text=False))
    return out


def insert_text_event(text: str) -> Event:
    """``Input.insertText`` —— **直接文本提交的 fallback**。

    它**不产生** ``compositionstart`` / ``compositionupdate`` / ``compositionend``。
    真实 IME 输入会产生这三个事件，认真做行为检测的站点能区分。用于非 ASCII 是权衡
    之后的选择（逐键 keydown 打中文更不真实），不是"更像真人"。真 IME 语义需要
    ``Input.imeSetComposition``，那是后续工作。

    :param text: 要一次性提交的文本
    """
    return Event("Input.insertText", {"text": text})


def type_events(
    text: str, *, rng: Random, profile: HumanProfile | None
) -> tuple[list[Event], float]:
    """逐字符输入。返回 ``(事件序列, 首个事件之前要等的秒数)``。

    非 ASCII 会被**成段**聚合成一次 ``insertText``，而不是一个字符一次 —— 后者既慢
    又不像任何真实输入法（IME 是整段上屏的）。段内各字符的间隔会累加成上屏前的等待。

    之所以要把 lead delay 单独返回：间隔靠挂在**上一个**事件的 ``sleep_after`` 上实现，
    而文本以非 ASCII 开头时还没有"上一个事件"。直接丢弃的后果是纯中文串产出一个
    ``sleep_after=0`` 的 insertText —— 实测「你好世界」应等约 820 ms，实际 0 ms，
    整段瞬间上屏。

    :param text: 要输入的文本，可混排 ASCII 与非 ASCII
    :param rng: 随机源
    :param profile: 拟人参数；``None`` = 直通（所有间隔为 0）
    :returns: ``(事件序列, lead_delay)``。``lead_delay`` 是派发第一个事件**之前**
        要 sleep 的秒数，由 :class:`~sleight.core.input.InputDriver` 负责睡掉
    """
    if not text:
        return [], 0.0

    out: list[Event] = []
    lead = 0.0

    def wait_before(seconds: float) -> None:
        """把间隔挂到**上一个**事件的 sleep_after 上。

        间隔是"按下之前等多久"，所以它属于前一个事件的尾巴。还没有前一个事件时
        攒进 ``lead``，由调用方在派发前 sleep 掉。
        """
        nonlocal lead
        if seconds <= 0.0:
            return
        if out:
            last = out[-1]
            out[-1] = Event(last.method, last.params, last.sleep_after + seconds)
        else:
            lead += seconds

    delays = (
        type_delays(text, rng=rng, profile=profile) if profile is not None else [0.0] * len(text)
    )

    buffer: list[str] = []
    buffered_delay = 0.0
    held = 0                              # 当前按住的修饰键

    def set_modifiers(wanted: int) -> None:
        """把按住的修饰键切换到 ``wanted``。

        **按住一段，而不是每个字符重按一次** —— 打 "HI" 的人是按住 Shift 打两下，
        不是 Shift 按下抬起两轮。
        """
        nonlocal held
        if (release := held & ~wanted):
            out.extend(modifier_events(release, pressed=False, active=held))
            held &= ~release
        if (press := wanted & ~held):
            out.extend(modifier_events(press, pressed=True, active=held))
            held |= press

    def flush() -> None:
        nonlocal buffered_delay
        if buffer:
            set_modifiers(0)              # insertText 不该带着 Shift
            wait_before(buffered_delay)
            out.append(insert_text_event("".join(buffer)))
            buffer.clear()
            buffered_delay = 0.0

    for ch, delay in zip(text, delays, strict=True):
        keydef = CHAR_MAP.get(ch)
        if keydef is None:
            # 攒起来，连续的非 ASCII 一次提交
            buffer.append(ch)
            buffered_delay += delay
            continue
        flush()
        wait_before(delay)
        set_modifiers(keydef.modifiers)
        out.extend(
            key_events(
                keydef,
                modifiers=keydef.modifiers,
                dwell=key_dwell(rng, profile) if profile is not None else 0.0,
            )
        )
    flush()
    set_modifiers(0)                      # 收尾必须把 Shift 松开
    return out, lead


def chord_events(chord: str, *, rng: Random, profile: HumanProfile | None) -> list[Event]:
    """``press("Enter")`` / ``press("Ctrl+A")``。

    修饰键自己的 keyDown / keyUp 会真的发出去，而不是只设 modifiers 位（见
    :func:`modifier_events`）。

    :param chord: 键名或组合键，``+`` 分隔，大小写不敏感。
        ``"Enter"`` / ``"Ctrl+A"`` / ``"ctrl+shift+k"``
    :param rng: 随机源
    :param profile: 取 ``key_dwell``；``None`` = 直通
    :returns: 按下修饰键 → 主键 down/up → 逆序抬起修饰键
    :raises ValueError: 键名不认识
    """
    keydef, modifiers = parse_chord(chord)
    dwell = key_dwell(rng, profile) if profile is not None else 0.0
    return [
        *modifier_events(modifiers, pressed=True, active=0),
        *key_events(keydef, modifiers=modifiers, dwell=dwell),
        *modifier_events(modifiers, pressed=False, active=modifiers),
    ]
