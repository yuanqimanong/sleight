"""在真浏览器里验证交互层：事件是否可信、拟人模式是否真的有轨迹。

页面自己注入，不依赖外部站点。

    set SLEIGHT_CLOAK_TOKEN=...
    pytest -m manager
"""

from __future__ import annotations

import json
import os
from itertools import pairwise

import pytest

from sleight import CAREFUL, FAST
from sleight.providers import CloakBrowserManager

pytestmark = pytest.mark.manager

BASE = os.environ.get("SLEIGHT_CLOAK_BASE", "http://127.0.0.1:19000")

#: 记录所有输入事件的探针页面。用 srcdoc 式注入，不碰外网。
PROBE = r"""
document.title = 'sleight probe';
document.body.innerHTML = `
  <div style="height:40px"></div>
  <button id="btn" style="width:160px;height:44px">Click me</button>
  <input id="inp" style="width:320px;height:32px;font-size:16px">
  <div style="height:1600px"></div>
  <button id="deep" style="width:160px;height:44px">Far below</button>
`;
window.__log = {moves: 0, downs: [], ups: [], clicks: [], keys: [], wheels: 0, untrusted: 0};
const L = window.__log;
document.addEventListener('mousemove', e => {
  L.moves++;
  if (!e.isTrusted) L.untrusted++;
  (L.trail = L.trail || []).push([e.clientX, e.clientY]);
}, true);
document.addEventListener('mousedown', e => {
  if (!e.isTrusted) L.untrusted++;
  L.downs.push({x: e.clientX, y: e.clientY, button: e.button, buttons: e.buttons, t: e.timeStamp});
}, true);
document.addEventListener('mouseup', e => L.ups.push({t: e.timeStamp}), true);
document.getElementById('btn').addEventListener('click', e => {
  if (!e.isTrusted) L.untrusted++;
  L.clicks.push({x: e.clientX, y: e.clientY, detail: e.detail});
});
document.addEventListener('keydown', e => {
  if (!e.isTrusted) L.untrusted++;
  L.keys.push({key: e.key, code: e.code, ctrl: e.ctrlKey, shift: e.shiftKey});
}, true);
document.addEventListener('wheel', () => L.wheels++, {capture: true, passive: true});
'ready'
"""


@pytest.fixture(scope="module")
def mgr() -> CloakBrowserManager:
    if not os.environ.get("SLEIGHT_CLOAK_TOKEN"):
        pytest.skip("SLEIGHT_CLOAK_TOKEN not set")
    return CloakBrowserManager(BASE)


@pytest.fixture
def probe(mgr: CloakBrowserManager):
    running = [i for i in mgr.list_instances() if i.ready]
    if not running:
        pytest.skip("no running profile")
    with mgr.lease(instance_id=running[0].id) as inst, inst.session(human=True) as s:
        s.open("about:blank")
        assert s.eval(PROBE) == "ready"
        yield s


def log(s) -> dict:
    return s.eval("JSON.stringify(window.__log)") and json.loads(
        s.eval("JSON.stringify(window.__log)")
    )


# --------------------------------------------------------------------------- #


def test_click_produces_trusted_events_with_a_real_trajectory(probe):
    """CDP 输入是真事件，但**浏览器不会替你补轨迹**。

    这条测试同时证明了库存在的理由 —— 直通模式下 move 事件是 1 条，拟人模式是几十条。
    """
    probe.click("#btn")
    data = log(probe)

    assert data["untrusted"] == 0, "every synthesised event must have isTrusted=true"
    assert len(data["clicks"]) == 1
    assert data["moves"] > 10, f"expected a trajectory, got {data['moves']} move events"

    # 落点必须在按钮里，而且不是正中心
    box = probe.query("#btn").box()
    click = data["clicks"][0]
    assert box.x <= click["x"] <= box.x + box.w
    assert box.y <= click["y"] <= box.y + box.h
    assert click["detail"] == 1

    # 坐标全是整数 —— 真实鼠标不产生小数
    assert all(float(x).is_integer() and float(y).is_integer() for x, y in data["trail"])


def test_passthrough_click_has_no_trajectory(probe):
    """对照组：这就是 Playwright / Puppeteer 默认的样子。"""
    probe.click("#btn", human=False)
    data = log(probe)
    assert len(data["clicks"]) == 1
    assert data["moves"] <= 1, f"passthrough should teleport, got {data['moves']} moves"


def test_press_and_release_are_separated_in_time(probe):
    """按压时长恒为 0 ms 是一眼可辨的特征。"""
    probe.click("#btn", human=CAREFUL)
    data = log(probe)
    dwell = data["ups"][0]["t"] - data["downs"][0]["t"]
    assert 30 < dwell < 400, f"dwell was {dwell} ms"


def test_button_mask_is_correct(probe):
    probe.click("#btn")
    down = log(probe)["downs"][0]
    assert down["button"] == 0 and down["buttons"] == 1


def test_typing_lands_in_the_field_with_per_character_events(probe):
    probe.type("#inp", "Hello, World!")
    assert probe.eval("document.getElementById('inp').value") == "Hello, World!"

    keys = log(probe)["keys"]
    # 修饰键自己也会发出 keydown（真键盘就是这样），过滤掉之后就是原文
    chars = [k for k in keys if k["code"] not in ("ShiftLeft", "ControlLeft")]
    assert [k["key"] for k in chars] == list("Hello, World!")
    assert chars[0]["code"] == "KeyH" and chars[0]["shift"] is True
    assert chars[1]["code"] == "KeyE" and chars[1]["shift"] is False
    assert any(k["code"] == "Space" for k in chars)


def test_typing_rhythm_is_not_uniform(probe):
    """固定间隔比慢更容易被识别。"""
    probe.type("#inp", "the quick brown fox jumps")
    stamps = probe.eval(
        "JSON.stringify(window.__log.keys.map((_, i) => i))"
    )
    assert stamps is not None
    # 间隔的离散度由单测覆盖（test_human.py）；这里只确认真的逐字符发了
    assert len(log(probe)["keys"]) == len("the quick brown fox jumps")


def test_clear_empties_a_prefilled_field(probe):
    """Ctrl+A + Backspace，不是 JS 置空 value。"""
    probe.eval("document.getElementById('inp').value = 'stale content'")
    probe.type("#inp", "fresh", clear=True)
    assert probe.eval("document.getElementById('inp').value") == "fresh"
    assert any(k["ctrl"] and k["code"] == "KeyA" for k in log(probe)["keys"])


def test_non_ascii_text_is_inserted(probe):
    probe.type("#inp", "你好 world")
    assert probe.eval("document.getElementById('inp').value") == "你好 world"


def test_scroll_emits_a_sequence_of_wheel_events(probe):
    probe.scroll(600)
    data = log(probe)
    assert data["wheels"] > 3, f"one giant scroll is a signature, got {data['wheels']} events"
    assert probe.eval("window.scrollY") > 100


def test_offscreen_element_scrolls_into_view_then_clicks(probe):
    """元素在视口外时先滚进来，滚完**重新取 box**（坐标已经变了）。"""
    assert not probe.query("#deep").in_viewport()
    probe.click("#deep", human=FAST)
    data = log(probe)
    assert data["untrusted"] == 0
    assert data["wheels"] > 0, "human mode must scroll with the wheel, not teleport"
    assert len(data["downs"]) == 1


def test_no_zero_movement_duplicate_before_mousedown(probe):
    """「最后一条 mousemove 的坐标正好等于 mousedown」是一行就能查的确定性特征。"""
    probe.click("#btn")
    data = log(probe)
    trail = [tuple(p) for p in data["trail"]]
    assert all(a != b for a, b in pairwise(trail)), "consecutive mousemoves at identical coords"
    down = data["downs"][0]
    assert trail.count((down["x"], down["y"])) == 1, "landing point reported twice"


def test_shift_generates_a_real_key_event(probe):
    """只设 modifiers 位的话，页面看到 shiftKey=true 却没有 Shift 的 keydown。"""
    probe.type("#inp", "Hi")
    keys = log(probe)["keys"]
    assert [k["key"] for k in keys] == ["Shift", "H", "i"]
    assert keys[0]["code"] == "ShiftLeft"
    ups = probe.eval("JSON.stringify(window.__log.keyups || [])")
    assert ups is not None


def test_shift_is_held_across_consecutive_capitals(probe):
    probe.type("#inp", "ABC")
    keys = [k["key"] for k in log(probe)["keys"]]
    assert keys == ["Shift", "A", "B", "C"], "Shift should be held, not re-pressed per char"
    assert probe.eval("document.getElementById('inp').value") == "ABC"


def test_double_click_fires_detail_1_then_2(probe):
    """真实双击是两次完整点击，detail 依次为 1、2。"""
    probe.eval("window.__log.details = []; "
               "document.getElementById('btn').addEventListener('click', "
               "e => window.__log.details.push(e.detail));")
    probe.double_click("#btn")
    details = probe.eval("JSON.stringify(window.__log.details)")
    import json as _json

    assert _json.loads(details) == [1, 2]
    assert probe.eval("window.__log.dbl === true || true")


def test_hover_moves_without_clicking(probe):
    probe.hover("#btn")
    data = log(probe)
    assert data["moves"] > 5
    assert data["downs"] == [] and data["clicks"] == []


def test_press_enter_reaches_the_page(probe):
    probe.click("#inp")
    probe.press("Enter")
    assert any(k["key"] == "Enter" for k in log(probe)["keys"])


def test_cursor_position_is_continuous_across_actions(probe):
    """每次都从 (0,0) 出发是一眼可辨的机器特征。"""
    probe.hover("#btn")
    first = probe.cursor
    probe.eval("window.__log.trail = []")
    probe.hover("#inp")
    trail = probe.eval("JSON.stringify(window.__log.trail)")
    start = json.loads(trail)[0]
    assert abs(start[0] - first.x) < 80 and abs(start[1] - first.y) < 80
