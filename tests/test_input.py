"""InputDriver：真正把事件发出去的那一层。"""

from __future__ import annotations

from random import Random

import pytest

from sleight.core.element import Element
from sleight.core.errors import ElementError
from sleight.core.human import engine
from sleight.core.human.presets import CAREFUL, DEFAULT, FAST
from sleight.core.input import InputDriver, resolve_profile
from sleight.core.keymap import MOD_CTRL, MOD_SHIFT
from sleight.core.types import Box, Point

from .conftest import FakeSession

MODIFIER_CODES = {"ShiftLeft", "ControlLeft", "AltLeft", "MetaLeft"}


def driver(session: FakeSession, **kw) -> InputDriver:
    kw.setdefault("rng", Random(11))
    return InputDriver(session, **kw)


def key_downs(session: FakeSession, *, modifiers: bool = False) -> list[dict]:
    """发出去的 keyDown。默认只要字符键，不含修饰键自己的按下。"""
    return [
        p for m, p in session.transport.sent
        if m == "Input.dispatchKeyEvent" and p["type"] == "keyDown"
        and (modifiers or p["code"] not in MODIFIER_CODES)
    ]


def modifier_events(session: FakeSession) -> list[tuple[str, str]]:
    return [
        (p["type"], p["code"]) for m, p in session.transport.sent
        if m == "Input.dispatchKeyEvent" and p["code"] in MODIFIER_CODES
    ]


# --------------------------------------------------------------------------- #
# human 三态开关
# --------------------------------------------------------------------------- #


def test_resolve_profile_rules():
    assert resolve_profile(None, False) is None            # 继承默认（直通）
    assert resolve_profile(None, True) is DEFAULT          # 继承默认（拟人）
    assert resolve_profile(None, CAREFUL) is CAREFUL
    assert resolve_profile(False, CAREFUL) is None         # 单句关闭压过默认
    assert resolve_profile(True, False) is DEFAULT         # 单句开启压过默认
    assert resolve_profile(FAST, False) is FAST


def test_resolve_profile_rejects_nonsense():
    with pytest.raises(TypeError, match="HumanProfile"):
        resolve_profile("careful", False)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# 传输方式 —— 轨迹段绝不能等响应
# --------------------------------------------------------------------------- #


def test_input_events_never_wait_for_a_response(session: FakeSession):
    """每个轨迹点都等响应 = 一次点击 20–60 次 RTT，本地丝滑远程慢动作。"""
    driver(session, default_human=True).click("#go")

    assert not [m for m, _ in session.transport.called if m.startswith("Input.")], (
        "input events must go through send_no_wait, not call"
    )
    assert len(session.transport.sent) > 10
    assert session.transport.flushes >= 1, "flush is the barrier and the only error surface"


def test_passthrough_mode_emits_a_single_move(session: FakeSession):
    """CDP 输入是真事件，但浏览器**不会替你补轨迹** —— 直通就是 0 条中间 move。"""
    driver(session, default_human=False).click("#go")
    assert session.transport.types() == ["mouseMoved", "mousePressed", "mouseReleased"]


def test_human_mode_emits_a_real_trajectory(session: FakeSession):
    driver(session, default_human=True).click("#go")
    kinds = session.transport.types()
    assert kinds.count("mouseMoved") > 8
    assert kinds[-2:] == ["mousePressed", "mouseReleased"]


# --------------------------------------------------------------------------- #
# 点击
# --------------------------------------------------------------------------- #


def test_click_event_parameters(session: FakeSession):
    driver(session, default_human=False).click("#go")
    events = dict((p.get("type"), p) for _, p in session.transport.sent)

    assert events["mouseMoved"]["buttons"] == 0
    assert events["mousePressed"]["buttons"] == 1
    assert events["mousePressed"]["button"] == "left"
    assert events["mousePressed"]["clickCount"] == 1     # 缺了部分站点不触发
    assert events["mouseReleased"]["buttons"] == 0
    assert all(isinstance(p["x"], int) and isinstance(p["y"], int)
               for p in events.values())


def test_right_click_uses_the_correct_button_mask(session: FakeSession):
    driver(session, default_human=False).click("#go", button="right")
    pressed = next(p for _, p in session.transport.sent if p.get("type") == "mousePressed")
    assert pressed["button"] == "right" and pressed["buttons"] == 2


def test_double_click_emits_two_complete_clicks(session: FakeSession):
    """真实双击是 detail 1 再 detail 2。

    把 clickCount=2 塞进单次 press/release 的话，页面永远收不到 detail=1 的 click，
    而且第一下 mousedown 就带 detail=2 —— 物理上不可能。
    """
    driver(session, default_human=False).click("#go", click_count=2)
    clicks = [
        (p["type"], p["clickCount"]) for _, p in session.transport.sent
        if p.get("type") in ("mousePressed", "mouseReleased")
    ]
    assert clicks == [
        ("mousePressed", 1), ("mouseReleased", 1),
        ("mousePressed", 2), ("mouseReleased", 2),
    ]


def test_unknown_button_is_rejected(session: FakeSession):
    with pytest.raises(ValueError, match="unknown mouse button"):
        driver(session, default_human=False).click("#go", button="thumb")


def test_landing_point_is_inside_the_element(session: FakeSession):
    d = driver(session, default_human=True)
    landing = d.click("#go")
    box = session.box
    assert box.x <= landing.x <= box.x + box.w
    assert box.y <= landing.y <= box.y + box.h


# --------------------------------------------------------------------------- #
# 命中校验 —— 两次，不是一次
# --------------------------------------------------------------------------- #


def test_hit_test_runs_twice(session: FakeSession):
    """轨迹要跑 300 ms – 2 s，期间弹窗可能盖住目标。只查一次等于没查。"""
    driver(session, default_human=True).click("#go")
    assert session.hit_tests == 2


def test_element_covered_before_moving_is_caught(session: FakeSession):
    session._hits = [False]
    with pytest.raises(ElementError, match="before moving there"):
        driver(session, default_human=True).click("#go")
    assert not session.transport.sent, "nothing should be dispatched if the target is covered"


def test_popup_appearing_mid_trajectory_is_caught(session: FakeSession):
    """第一次校验通过、轨迹跑完后被盖住 —— 这正是第二次校验存在的理由。"""
    session._hits = [True, False]
    with pytest.raises(ElementError, match="after the pointer arrived"):
        driver(session, default_human=True).click("#go")

    kinds = session.transport.types()
    assert "mouseMoved" in kinds
    assert "mousePressed" not in kinds, "must not click through an overlay"


def test_blocking_element_is_named_in_the_error(session: FakeSession):
    session._hits = [False]
    with pytest.raises(ElementError, match="cookie-banner"):
        driver(session, default_human=True).click("#go")


def test_both_hit_tests_probe_the_actual_landing_point(session: FakeSession):
    """探错点的命中校验等于没有校验 —— 光数次数抓不到这个。"""
    landing = driver(session, default_human=True).click("#go")
    assert session.probes == [(landing.x, landing.y), (landing.x, landing.y)]
    assert landing != session.box.center, "landing should not be the dead centre"


def test_raw_coordinates_skip_hit_testing(session: FakeSession):
    """给的是坐标不是元素，没有"目标元素"可查。"""
    driver(session, default_human=False).click(Point(10, 20))
    assert session.hit_tests == 0
    moved = next(p for _, p in session.transport.sent if p.get("type") == "mouseMoved")
    assert (moved["x"], moved["y"]) == (10, 20)


# --------------------------------------------------------------------------- #
# 鼠标位置连续性
# --------------------------------------------------------------------------- #


def test_cursor_starts_somewhere_in_the_viewport_not_origin(session: FakeSession):
    d = driver(session)
    assert d.cursor != Point(0, 0)
    w, h = session.viewport()
    assert 0 < d.cursor.x < w and 0 < d.cursor.y < h


def test_second_move_starts_where_the_first_ended(session: FakeSession):
    d = driver(session, default_human=True)
    first = d.click("#a")
    assert d.cursor == first

    session.transport.sent.clear()
    d.click("#b")
    start = session.transport.sent[0][1]
    assert (start["x"], start["y"]) != (0, 0)
    # 第一个点应当在上一次落点附近，而不是从头开始
    assert abs(start["x"] - first.x) < 60 and abs(start["y"] - first.y) < 60


# --------------------------------------------------------------------------- #
# 打字
# --------------------------------------------------------------------------- #


def test_type_focuses_by_clicking_first(session: FakeSession):
    driver(session, default_human=False).type("#email", "ab")
    assert session.transport.types()[:3] == ["mouseMoved", "mousePressed", "mouseReleased"]


def test_printable_characters_carry_text(session: FakeSession):
    driver(session, default_human=False).type(None, "ab")
    downs = key_downs(session)
    assert [p["text"] for p in downs] == ["a", "b"]
    assert [p["code"] for p in downs] == ["KeyA", "KeyB"]
    assert [p["windowsVirtualKeyCode"] for p in downs] == [65, 66]


def test_every_keydown_has_a_matching_keyup(session: FakeSession):
    driver(session, default_human=False).type(None, "hello")
    kinds = [p["type"] for m, p in session.transport.sent if m == "Input.dispatchKeyEvent"]
    assert kinds == ["keyDown", "keyUp"] * 5


def test_uppercase_uses_shift_and_unmodified_text(session: FakeSession):
    driver(session, default_human=False).type(None, "A")
    down = key_downs(session)[0]
    assert down["modifiers"] == MOD_SHIFT
    assert down["text"] == "A" and down["unmodifiedText"] == "a"
    assert down["code"] == "KeyA"


def test_shift_itself_is_pressed_and_released(session: FakeSession):
    """只设 modifiers 位的话，页面会看到 shiftKey=true 却没有 Shift 的 keydown。"""
    driver(session, default_human=False).type(None, "A")
    assert modifier_events(session) == [("keyDown", "ShiftLeft"), ("keyUp", "ShiftLeft")]


def test_shift_is_held_across_a_run_of_capitals(session: FakeSession):
    """打 "HI" 的人按住 Shift 打两下，不是按下抬起两轮。"""
    driver(session, default_human=False).type(None, "HI")
    assert modifier_events(session) == [("keyDown", "ShiftLeft"), ("keyUp", "ShiftLeft")]
    assert [p["code"] for p in key_downs(session)] == ["KeyH", "KeyI"]


def test_shift_is_released_between_runs(session: FakeSession):
    driver(session, default_human=False).type(None, "Ab")
    assert modifier_events(session) == [("keyDown", "ShiftLeft"), ("keyUp", "ShiftLeft")]
    order = [
        p["code"] for m, p in session.transport.sent
        if m == "Input.dispatchKeyEvent" and p["type"] == "keyDown"
    ]
    assert order == ["ShiftLeft", "KeyA", "KeyB"]


def test_shifted_punctuation(session: FakeSession):
    driver(session, default_human=False).type(None, "!")
    down = key_downs(session)[0]
    assert down["code"] == "Digit1" and down["text"] == "!" and down["modifiers"] == MOD_SHIFT


def test_non_ascii_is_batched_into_one_insert_text(session: FakeSession):
    """IME 是整段上屏的，一个汉字一次 insertText 既慢又不像任何真实输入法。"""
    driver(session, default_human=False).type(None, "你好世界")
    inserts = [p for m, p in session.transport.sent if m == "Input.insertText"]
    assert len(inserts) == 1 and inserts[0]["text"] == "你好世界"


def test_mixed_scripts_interleave_correctly(session: FakeSession):
    driver(session, default_human=False).type(None, "a你好b")
    assert session.transport.types() == [
        "keyDown", "keyUp", "insertText", "keyDown", "keyUp",
    ]


def test_clear_uses_select_all_and_backspace_not_js(session: FakeSession):
    """JS 置空 value 不触发 input 事件，React 之类的受控组件读不到。"""
    driver(session, default_human=False).type("#email", "x", clear=True)
    downs = key_downs(session)
    assert downs[0]["code"] == "KeyA" and downs[0]["modifiers"] == MOD_CTRL
    assert downs[1]["code"] == "Backspace"
    assert ("keyDown", "ControlLeft") in modifier_events(session)


def test_clear_refuses_to_run_without_focus(session: FakeSession):
    """Ctrl+A + Backspace 是破坏性的 —— 焦点没落对就是清空了**别的**输入框。

    点击只保证几何命中；点一个不可聚焦的 <div> 会让焦点留在原处。
    """
    session.focused = False
    with pytest.raises(ElementError, match="does not have focus"):
        driver(session, default_human=False).type("#not-an-input", "x", clear=True)
    assert not any(
        p.get("code") == "Backspace" for _, p in session.transport.sent
    ), "must not destroy content in whatever had focus"


def test_typing_without_clear_does_not_require_focus(session: FakeSession):
    """只是补字的话不做这个检查 —— 非破坏性，也不该平白多一次往返。"""
    session.focused = False
    driver(session, default_human=False).type("#go", "x")
    assert any(p.get("type") == "keyDown" for _, p in session.transport.sent)


def test_ctrl_chords_do_not_insert_text(session: FakeSession):
    """带 text 的 Ctrl+A 会在全选的同时插入一个 'a'。"""
    driver(session, default_human=False).press("Ctrl+A")
    assert "text" not in key_downs(session)[0]


def test_named_keys_and_modifier_chords(session: FakeSession):
    d = driver(session, default_human=False)
    d.press("Enter")
    down = key_downs(session)[0]
    assert down["key"] == "Enter" and down["windowsVirtualKeyCode"] == 13
    assert modifier_events(session) == []      # 无修饰键就不该有多余事件

    session.transport.sent.clear()
    d.press("ctrl+shift+k")
    down = key_downs(session)[0]
    assert down["modifiers"] == MOD_CTRL | MOD_SHIFT and down["code"] == "KeyK"
    # 修饰键按下顺序 Ctrl → Shift，抬起逆序
    assert modifier_events(session) == [
        ("keyDown", "ControlLeft"), ("keyDown", "ShiftLeft"),
        ("keyUp", "ShiftLeft"), ("keyUp", "ControlLeft"),
    ]


def test_modifier_state_is_reported_correctly_while_stacking(session: FakeSession):
    """按下 Ctrl 那一刻 modifiers 里已经有 Ctrl，还没有 Shift。"""
    driver(session, default_human=False).press("ctrl+shift+k")
    mods = [
        (p["code"], p.get("modifiers", 0)) for m, p in session.transport.sent
        if m == "Input.dispatchKeyEvent" and p["type"] == "keyDown"
    ]
    assert mods == [("ControlLeft", MOD_CTRL), ("ShiftLeft", MOD_CTRL | MOD_SHIFT),
                    ("KeyK", MOD_CTRL | MOD_SHIFT)]


def test_unknown_key_gives_an_actionable_error(session: FakeSession):
    with pytest.raises(ValueError, match="known named keys"):
        driver(session, default_human=False).press("Wingding")


# --------------------------------------------------------------------------- #
# 滚动
# --------------------------------------------------------------------------- #


def test_human_scroll_is_broken_into_steps(session: FakeSession):
    driver(session, default_human=True).scroll(900)
    wheels = [p for _, p in session.transport.sent if p.get("type") == "mouseWheel"]
    assert len(wheels) > 5
    assert sum(w["deltaY"] for w in wheels) == 900
    assert all(w["deltaX"] == 0 for w in wheels)


def test_passthrough_scroll_is_one_event(session: FakeSession):
    driver(session, default_human=False).scroll(900)
    wheels = [p for _, p in session.transport.sent if p.get("type") == "mouseWheel"]
    assert len(wheels) == 1 and wheels[0]["deltaY"] == 900


def offscreen(session: FakeSession, y: float = 2400.0) -> None:
    """把元素挪到视口下方很远的地方（文档坐标）。"""
    session.doc_box = Box(400.0, y, 120.0, 40.0)


def test_passthrough_scroll_into_view_uses_the_dom_command(session: FakeSession):
    """``DOM.scrollIntoViewIfNeeded`` —— CDP 没有 Element domain。"""
    offscreen(session)
    driver(session, default_human=False).scroll_into_view("#deep")
    assert session.scrolled_instantly == 1
    assert session.released_objects == 1, "RemoteObject handles must be released"


def test_offscreen_element_is_scrolled_before_clicking(session: FakeSession):
    offscreen(session)
    driver(session, default_human=False).click("#deep")
    assert session.scrolled_instantly == 1


def test_human_click_on_offscreen_element_scrolls_with_the_wheel(session: FakeSession):
    """拟人模式下滚动也要留下滚轮事件序列，不能瞬移。"""
    offscreen(session)
    driver(session, default_human=True).click("#deep")
    assert session.scrolled_instantly == 0, "human mode must not use the instant CDP command"
    assert [p for _, p in session.transport.sent if p.get("type") == "mouseWheel"]
    assert session.box.y < session._viewport[1], "the element really moved into view"


def test_click_uses_the_post_scroll_box(session: FakeSession):
    """滚完必须**重新取 box** —— 坐标已经变了。"""
    offscreen(session)
    d = driver(session, default_human=False)
    d.click("#deep")
    pressed = next(p for _, p in session.transport.sent if p.get("type") == "mousePressed")
    box = session.box                       # 滚动之后的视口坐标
    assert box.y <= pressed["y"] <= box.y + box.h, (
        "pressed at the pre-scroll coordinates — the box was not re-read"
    )


def test_scroll_gives_up_when_nothing_moves(session: FakeSession):
    """滚轮被嵌套容器吃掉时要立刻放弃，而不是重发 8 次上百个事件。"""
    offscreen(session)
    session.scrollable = False              # 滚轮打进黑洞
    with pytest.raises(ElementError, match="nested scroll container"):
        driver(session, default_human=True).scroll_into_view("#deep")
    wheels = [p for _, p in session.transport.sent if p.get("type") == "mouseWheel"]
    assert len(wheels) < 40, f"blasted {len(wheels)} wheel events at an unscrollable element"


# --------------------------------------------------------------------------- #
# 拖拽
# --------------------------------------------------------------------------- #


def mouse(session: FakeSession) -> list[dict]:
    return [p for m, p in session.transport.sent if m == "Input.dispatchMouseEvent"]


def drag_path(session: FakeSession) -> list[dict]:
    """按住键的那段轨迹 —— buttons 非零的 mouseMoved。"""
    return [p for p in mouse(session) if p["type"] == "mouseMoved" and p["buttons"]]


def test_drag_needs_exactly_one_destination(session: FakeSession):
    d = driver(session)
    with pytest.raises(ValueError, match="exactly one"):
        d.drag("#h")
    with pytest.raises(ValueError, match="exactly one"):
        d.drag("#h", to="#t", by=(10, 0))


def test_drag_holds_the_button_down_for_the_whole_haul(session: FakeSession):
    """拖拽段的 buttons 掩码一路非零。

    掩码回 0 的移动就是 hover —— 页面 handler 一句 ``e.buttons`` 就知道键没按着，
    整条轨迹白发。
    """
    driver(session, default_human=True).drag("#handle", by=(200, 0))

    events = mouse(session)
    press = next(i for i, p in enumerate(events) if p["type"] == "mousePressed")
    release = next(i for i, p in enumerate(events) if p["type"] == "mouseReleased")
    haul = events[press + 1 : release]

    assert haul, "no movement between press and release — that is a click, not a drag"
    assert all(p["type"] == "mouseMoved" for p in haul)
    assert all(p["buttons"] == 1 for p in haul), "the button went up mid-haul"


def test_drag_by_offset_lands_where_asked(session: FakeSession):
    d = driver(session, default_human=True)
    grabbed = session.box.center
    landed = d.drag("#handle", by=(200, -30))

    press = next(p for p in mouse(session) if p["type"] == "mousePressed")
    assert landed == Point(press["x"] + 200, press["y"] - 30)
    assert abs(press["x"] - grabbed.x) <= session.box.w, "grabbed nowhere near the element"


def test_drag_to_an_element_ends_on_it(session: FakeSession):
    target = Element(session, "#drop")
    landed = driver(session, default_human=True).drag("#handle", to=target)
    box = session.box
    assert box.x <= landed.x <= box.x + box.w
    assert box.y <= landed.y <= box.y + box.h


def test_drag_pauses_before_letting_go(session: FakeSession, monkeypatch):
    """到位即松手是机器最稳定的特征之一，滑块风控盯的就是这一段。"""
    slept: list[float] = []
    monkeypatch.setattr("sleight.core.input.time.sleep", slept.append)

    driver(session, default_human=DEFAULT).drag("#handle", by=(200, 0))

    lo, hi = DEFAULT.drag_settle
    assert any(lo <= s <= hi + DEFAULT.step_delay[1] for s in slept), (
        f"no pause in the drag_settle range before the release; slept {slept}"
    )


def test_drag_settle_is_not_a_repeated_move_event(session: FakeSession):
    """迟滞挂在最后一个轨迹点的尾巴上，不能补一条零位移的 mouseMoved 来承载它。

    补出来的那条位置**恒等于**落点，而 mouse_path 刚刚才把连续重复点去掉 ——
    一行 ``prev.x === cur.x && prev.y === cur.y`` 就能把整个库挑出来。
    """
    driver(session, default_human=DEFAULT).drag("#handle", by=(200, 0))
    haul = drag_path(session)
    assert (haul[-1]["x"], haul[-1]["y"]) != (haul[-2]["x"], haul[-2]["y"])


def test_engine_drag_events_hang_the_settle_on_the_last_move():
    events, _ = engine.drag_events(
        Point(10, 10), Box(100, 100, 40, 20), Box(600, 100, 40, 20),
        rng=Random(3), profile=DEFAULT,
    )
    moves = [e for e in events if e.params.get("type") == "mouseMoved" and e.params["buttons"]]
    assert moves[-1].sleep_after >= DEFAULT.drag_settle[0]
    assert events[-1].params["type"] == "mouseReleased"


def test_drag_refuses_an_offscreen_drop_target_instead_of_scrolling(session: FakeSession):
    """按住键滚页面会让已经抓在手里的 box 失效，而且没法还原成人类动作。"""
    d = driver(session, default_human=True)
    target = Element(session, "#drop")
    offscreen(session)                       # 现在两端都在视口外了
    with pytest.raises(ElementError, match="outside the viewport"):
        d.drag(Point(100, 100), to=target)
    assert not [p for p in mouse(session) if p["type"] == "mousePressed"], (
        "pressed the button and then failed — that leaves the mouse stuck down"
    )


def test_drag_overshoots_at_slider_distances():
    """指针那档 500 px 的阈值套到滑块上等于永不过冲。

    过冲是概率事件，所以这里统计 12 个种子 —— 单个种子的绿灯说明不了阈值改没改对。
    """
    overshot = 0
    for seed in range(12):
        s = FakeSession(box=Box(100.0, 300.0, 30.0, 30.0))
        InputDriver(s, default_human=CAREFUL, rng=Random(seed)).drag("#handle", by=(180, 0))
        xs = [p["x"] for p in drag_path(s)]
        overshot += max(xs) > xs[-1]
    assert overshot >= 9, (
        f"only {overshot}/12 slider drags overshot — the pointer-grade threshold is still "
        "in effect, and a strictly monotonic haul is itself the tell"
    )


def test_drag_hit_checks_the_source_but_not_the_destination(session: FakeSession):
    """被拖的元素通常就挂在光标底下，对终点做命中校验只会误报。"""
    driver(session, default_human=True).drag("#handle", to=Element(session, "#drop"))
    assert session.hit_tests == 2, f"{session.hit_tests} hit tests; expected exactly the 2 on grab"


# —— HTML5 原生拖放 ——

DND = {"items": [{"mimeType": "text/plain", "data": "card-7"}], "dragOperationsMask": 1}


def dnd(session: FakeSession) -> list[dict]:
    return [p for m, p in session.transport.sent if m == "Input.dispatchDragEvent"]


def test_drag_and_drop_translates_the_trajectory_when_the_browser_intercepts(
    session: FakeSession,
):
    """原生拖放期间页面收到的是 dragover，不是 mousemove —— 这里是翻译，不是追加。"""
    session.drag_data = DND
    driver(session, default_human=True).drag_and_drop("#card", "#column")

    types = [p["type"] for p in dnd(session)]
    assert types[0] == "dragEnter", "no dragEnter — drop zones that light up on it stay dark"
    assert types[-1] == "drop"
    assert types.count("dragOver") >= 1
    assert all(p["data"] == DND for p in dnd(session)), "dragIntercepted data must be echoed back"

    # 起步段之后不该再有 mouseMoved：原生拖放期间浏览器发的就是 drag 事件。两套并发
    # 会让同时监听 mousemove 和 dragover 的实现看到双倍的移动
    order = [
        m for m, p in session.transport.sent
        if m == "Input.dispatchDragEvent"
        or (m == "Input.dispatchMouseEvent" and p["type"] == "mouseMoved" and p["buttons"])
    ]
    first_drag = order.index("Input.dispatchDragEvent")
    assert "Input.dispatchMouseEvent" not in order[first_drag:], (
        "kept sending held mouseMoved events after the browser took over the drag"
    )
    assert first_drag <= 6, f"kickoff took {first_drag} moves; it only needs to clear ~5 px"


def test_drag_and_drop_falls_back_to_mouse_events_for_js_implementations(session: FakeSession):
    """没被拦截 = 页面是自己用 mousemove 实现的拖动。继续发鼠标事件就行。"""
    session.drag_data = None                 # 不是原生可拖元素
    driver(session, default_human=True).drag_and_drop("#card", "#column")

    assert not dnd(session), "dispatched drag events at an element the browser never intercepted"
    assert drag_path(session), "no mouse trajectory either — the drag did nothing at all"
    assert [p for p in mouse(session) if p["type"] == "mouseReleased"]


def test_drag_and_drop_always_turns_interception_back_off(session: FakeSession):
    session.drag_data = DND
    driver(session, default_human=True).drag_and_drop("#card", "#column")
    assert session.intercepting is False, "left setInterceptDrags on for the whole session"


def test_interception_is_turned_off_even_when_the_drag_blows_up(session: FakeSession):
    session.drag_data = DND
    session._hits = False                    # 起点被遮挡
    with pytest.raises(ElementError):
        driver(session, default_human=True).drag_and_drop("#card", "#column")
    assert session.intercepting is False


def test_an_old_chrome_without_setinterceptdrags_degrades_to_mouse_only(session: FakeSession):
    session.intercept_drags = False          # 命令不存在
    driver(session, default_human=True).drag_and_drop("#card", "#column")
    assert drag_path(session), "gave up entirely instead of falling back to mouse events"


def test_native_true_releases_the_button_before_complaining(session: FakeSession):
    """带着按下状态抛异常的话，之后每一次点击都会变成拖拽。"""
    session.drag_data = None                 # 并非原生可拖
    with pytest.raises(ElementError, match="not natively draggable"):
        driver(session, default_human=True).drag_and_drop("#card", "#column", native=True)

    types = [p["type"] for p in mouse(session)]
    assert types.count("mousePressed") == types.count("mouseReleased") == 1


def test_native_false_never_touches_the_drag_domain(session: FakeSession):
    session.drag_data = DND
    driver(session, default_human=True).drag_and_drop("#card", "#column", native=False)
    assert not dnd(session)
    assert "Input.setInterceptDrags" not in [m for m, _ in session.transport.called]


def test_held_moves_carry_button_not_just_the_buttons_mask():
    """真机上验出来的：只给 buttons 掩码，HTML5 原生拖放**一声不吭地不启动**。

    页面 JS 读的是 ``e.buttons``，所以自制滑块照常工作；而浏览器自己的拖拽控制器
    认的是 ``button`` —— 少了它 dragstart 不触发、``Input.dragIntercepted`` 永远不来，
    表现是"一切正常但什么也没发生"。Chrome 146 上实测：同一段轨迹加上
    ``button='left'`` 才有 dragstart。
    """
    held, _ = engine.move_events(
        Point(10, 10), Point(300, 10), rng=Random(1), profile=DEFAULT, held="left"
    )
    assert held, "no trajectory"
    assert all(e.params["button"] == "left" for e in held)
    assert all(e.params["buttons"] == 1 for e in held)

    # 直通模式也一样 —— 它只发一条，漏了同样不启动
    passthrough, _ = engine.move_events(
        Point(10, 10), Point(300, 10), rng=Random(1), profile=None, held="left"
    )
    assert passthrough[0].params["button"] == "left"


def test_plain_moves_do_not_claim_a_button():
    """普通 hover 带上 button 就是在谎报状态。"""
    moves, _ = engine.move_events(
        Point(10, 10), Point(300, 10), rng=Random(1), profile=DEFAULT
    )
    assert all("button" not in e.params for e in moves)
    assert all(e.params["buttons"] == 0 for e in moves)


def test_the_driver_sends_button_on_the_whole_haul(session: FakeSession):
    driver(session, default_human=True).drag("#handle", by=(200, 0))
    assert all(p.get("button") == "left" for p in drag_path(session)), (
        "a held move without button= will not start a native HTML5 drag"
    )
