"""InputDriver：真正把事件发出去的那一层。"""

from __future__ import annotations

from random import Random

import pytest

from sleight.core.errors import ElementError
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
