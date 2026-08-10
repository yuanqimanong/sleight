"""真浏览器下的 frame 观察层：枚举 frame 树、读进同源子 frame、跨源明确报错。

这些用例**必须**在真 Chromium 上跑 —— frame 树、执行上下文映射、跨源与否，都是
fake transport 编不出来的东西。没有浏览器时（CI 默认）整个模块 skip。

跑法：装了 chromium 直接 `pytest tests/test_integration_frames.py`；或
`SLEIGHT_TEST_CHROMIUM=/path/to/chrome pytest -m integration`。
"""

from __future__ import annotations

import pytest

from sleight.core.errors import SleightError
from sleight.core.types import Text

from .conftest import serve_pages

pytestmark = pytest.mark.integration


def test_frames_lists_the_main_frame_only_on_a_plain_page(live_session):
    with serve_pages({"plain.html": "<!doctype html><title>P</title><h1>Plain</h1>"}) as d:
        live_session.open(f"file://{d}/plain.html", wait=Text("Plain"))
        frames = live_session.frames()

    assert len(frames) == 1
    assert frames[0].is_main
    assert frames[0].parent_id is None
    assert frames[0].reachable


def test_frames_sees_a_same_origin_iframe_and_marks_it_reachable(live_session):
    pages = {
        "child.html": "<!doctype html><title>C</title><h1 id=inner>Inside iframe</h1>",
        "parent.html": (
            "<!doctype html><title>P</title><h1>Parent</h1>"
            "<iframe name='box' src='child.html'></iframe>"
        ),
    }
    with serve_pages(pages) as d:
        live_session.open(f"file://{d}/parent.html", wait=Text("Parent"))
        # 给子 frame 一点时间把 executionContextCreated 送到
        live_session.pump_events(0.5)
        frames = live_session.frames()

        assert len(frames) == 2, f"应看到主 frame + 子 iframe，实际 {frames}"
        child = next(f for f in frames if not f.is_main)
        assert child.parent_id == frames[0].frame_id
        assert child.name == "box"
        assert child.url.endswith("child.html")
        assert child.reachable, "同源 iframe 应可达"

        # 读进子 frame：拿到的是**它自己**的 DOM，不是父页
        view = live_session.frame("box")
        assert "Inside iframe" in view.text()
        assert view.count("#inner") == 1
        # 主 frame 里没有 #inner —— 证明作用域确实隔开了
        assert live_session.query("#inner") is None


def test_frame_lookup_miss_lists_available_child_frames(live_session):
    """找不到的 frame 要显式报错，并把有哪些子 frame 列出来帮定位。"""
    pages = {
        "child.html": "<!doctype html><title>C</title><h1>x</h1>",
        "parent.html": (
            "<!doctype html><title>P</title><h1>Parent</h1>"
            "<iframe name='box' src='child.html'></iframe>"
        ),
    }
    with serve_pages(pages) as d:
        live_session.open(f"file://{d}/parent.html", wait=Text("Parent"))
        live_session.pump_events(0.5)
        with pytest.raises(SleightError, match="no frame matches"):
            live_session.frame("does-not-exist")
