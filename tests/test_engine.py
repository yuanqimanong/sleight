"""事件编排层：鼠标 / 键盘 / 滚轮的 CDP 事件形状。

重点在 **sleep_after**：轨迹节奏全靠它，而在这个文件出现之前没有任何测试读过这个字段，
也就是说 ``profile.reaction`` / ``dwell`` / ``step_delay`` / ``scroll_delay``
可以被整段删掉而测试全绿。
"""

from __future__ import annotations

from itertools import pairwise
from random import Random

import pytest

from sleight.core.human import engine
from sleight.core.human.presets import CAREFUL, DEFAULT, FAST, HumanProfile
from sleight.core.keymap import MOD_CTRL
from sleight.core.types import Box, Point

BOX = Box(400.0, 300.0, 120.0, 40.0)
START = Point(60, 60)


def within(value: float, bounds: tuple[float, float]) -> bool:
    lo, hi = bounds
    return lo - 1e-9 <= value <= hi + 1e-9


# --------------------------------------------------------------------------- #
# 鼠标时序
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("profile", [FAST, DEFAULT, CAREFUL])
def test_every_trajectory_step_waits_within_step_delay(profile: HumanProfile):
    events, _ = engine.move_events(START, BOX, rng=Random(4), profile=profile)
    assert len(events) > 5
    for method, params, delay in events:
        assert method == "Input.dispatchMouseEvent" and params["type"] == "mouseMoved"
        assert within(delay, profile.step_delay), delay


@pytest.mark.parametrize("profile", [FAST, DEFAULT, CAREFUL])
def test_press_dwells_and_reaction_is_a_pure_wait(profile: HumanProfile):
    """按下到抬起不是 0 ms；反应时间是一段**纯等待**，不额外补一条 move。"""
    events = engine.press_events(Point(10, 10), button="left", rng=Random(4), profile=profile)
    assert len(events) == 1, "the reaction pause must not be carried by a phantom mouseMoved"
    (press,) = events
    assert press.params["type"] == "mousePressed"
    assert within(press.sleep_after, profile.dwell), press.sleep_after
    assert within(engine.reaction_delay(Random(4), profile), profile.reaction)


def test_no_zero_movement_duplicate_before_the_press():
    """``mouse_path`` 刚去掉连续重复点，不能在按下前又补回一个。

    「最后一条 mousemove 的坐标永远正好等于 mousedown 的坐标」是一行就能查出来的
    确定性特征，而且每次拟人点击都有。
    """
    events, landing = engine.click_events(START, BOX, rng=Random(7), profile=DEFAULT)
    moves = [e for e in events if e.params.get("type") == "mouseMoved"]
    coords = [(e.params["x"], e.params["y"]) for e in moves]
    assert all(a != b for a, b in pairwise(coords)), "consecutive mousemoves at identical coords"
    assert coords[-1] == (landing.x, landing.y)
    assert coords.count((landing.x, landing.y)) == 1, "landing point reported twice"


def test_passthrough_has_no_waits_at_all():
    events, _ = engine.move_events(START, BOX, rng=Random(4), profile=None)
    press = engine.press_events(Point(1, 1), button="left", rng=Random(4), profile=None)
    assert all(e.sleep_after == 0.0 for e in [*events, *press])
    assert engine.reaction_delay(Random(4), None) == 0.0
    assert len(events) == 1                       # 直通就是瞬移


def test_scroll_steps_carry_scroll_delay():
    events = engine.scroll_events(Point(5, 5), 900, rng=Random(2), profile=DEFAULT)
    assert len(events) > 5
    assert all(within(e.sleep_after, DEFAULT.scroll_delay) for e in events)
    assert sum(e.params["deltaY"] for e in events) == 900


def test_total_move_time_scales_with_the_profile():
    """CAREFUL 必须真的更慢 —— 不能只是点更多。"""
    def elapsed(profile: HumanProfile) -> float:
        events, _ = engine.move_events(START, BOX, rng=Random(9), profile=profile)
        return sum(e.sleep_after for e in events)

    assert elapsed(FAST) < elapsed(DEFAULT) < elapsed(CAREFUL)


# --------------------------------------------------------------------------- #
# 打字时序
# --------------------------------------------------------------------------- #


def test_keydown_holds_for_key_dwell():
    events, _ = engine.type_events("ab", rng=Random(3), profile=DEFAULT)
    downs = [e for e in events if e.params.get("type") == "keyDown"]
    assert len(downs) == 2
    assert all(within(e.sleep_after, DEFAULT.key_dwell) for e in downs)


def test_inter_key_gap_hangs_off_the_previous_keyup():
    """间隔是"按下之前等多久"，所以它属于**前一个**事件的尾巴。"""
    events, lead = engine.type_events("th", rng=Random(3), profile=DEFAULT)
    assert lead == 0.0                        # 首字符是 ASCII，没有前置等待
    kinds = [e.params.get("type") for e in events]
    assert kinds == ["keyDown", "keyUp", "keyDown", "keyUp"]
    assert events[1].sleep_after > 0.05, "the t->h gap vanished"


def test_leading_cjk_run_keeps_its_accumulated_delay():
    """纯中文串不能瞬间上屏。

    间隔靠挂在上一个事件上实现，而文本以非 ASCII 开头时还没有上一个事件。旧实现直接
    把它丢了：「你好世界」应等约 820 ms，实测 wall time 恒为 0。
    """
    events, lead = engine.type_events("你好世界", rng=Random(3), profile=DEFAULT)
    assert len(events) == 1 and events[0].method == "Input.insertText"
    assert lead > 0.2, f"the whole run's typing time was discarded (lead={lead})"

    _, long_lead = engine.type_events("这是一段比较长的中文", rng=Random(3), profile=DEFAULT)
    assert long_lead > lead, "longer text must take longer to compose"


def test_cjk_after_ascii_hangs_the_delay_on_the_previous_event():
    events, lead = engine.type_events("a你好", rng=Random(3), profile=DEFAULT)
    assert lead == 0.0
    assert events[-1].method == "Input.insertText"
    assert events[-2].sleep_after > 0.05, "the ascii->cjk gap vanished"


def test_passthrough_typing_has_no_waits():
    events, lead = engine.type_events("hello 你好", rng=Random(3), profile=None)
    assert lead == 0.0
    assert all(e.sleep_after == 0.0 for e in events)


def test_ctrl_chord_presses_the_modifier_itself():
    """只设 modifiers 位的话，页面看到 ctrlKey=true 却没有 Control 的 keydown。"""
    events = engine.chord_events("Ctrl+A", rng=Random(3), profile=DEFAULT)
    assert [(e.params["type"], e.params["code"]) for e in events] == [
        ("keyDown", "ControlLeft"),
        ("keyDown", "KeyA"),
        ("keyUp", "KeyA"),
        ("keyUp", "ControlLeft"),
    ]
    key_down = events[1]
    assert key_down.params["modifiers"] == MOD_CTRL
    assert "text" not in key_down.params, "Ctrl+A with text would also insert an 'a'"
    assert within(key_down.sleep_after, DEFAULT.key_dwell)


def test_bare_key_emits_no_modifier_events():
    events = engine.chord_events("Enter", rng=Random(3), profile=DEFAULT)
    assert [e.params["code"] for e in events] == ["Enter", "Enter"]


def test_empty_text_produces_nothing():
    assert engine.type_events("", rng=Random(1), profile=DEFAULT) == ([], 0.0)
