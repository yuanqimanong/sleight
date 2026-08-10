"""真浏览器下的四工具 Gateway：observe → find → act(ref/坐标) → extract 全链路。

结构化结果、Ref 解析、按 Ref 点击产生的真事件都得在真 Chromium 上验证。无浏览器时 skip。
"""

from __future__ import annotations

import pytest

from sleight.agent import Gateway

from .conftest import serve_pages

pytestmark = pytest.mark.integration

_PAGE = (
    "<!doctype html><html lang=en><title>T</title><h1>Head</h1>"
    "<button id=go onclick='window.__hit = event.isTrusted'>Submit order</button>"
    "<input id=q placeholder=search>"
    "<article><p>" + "Readable article prose with commas, and periods. " * 6 + "</p></article>"
)


def test_full_agent_loop(live_session):
    g = Gateway(live_session)
    with serve_pages({"p.html": _PAGE}) as d:
        assert g.session("open", url=f"file://{d}/p.html", wait_text="Head").ok

        snap = g.observe("snapshot")
        assert snap.ok and snap.data["refs"], "快照应给出可交互 Ref"

        found = g.find("submit")
        assert found.data["matches"], "应按文字找到按钮"
        button_ref = found.data["matches"][0]["ref"]

        assert g.act("click", ref=button_ref).ok
        assert live_session.eval("window.__hit") is True, "按 Ref 点击应产生可信事件"

        textbox = g.find("search", role="textbox").data["matches"][0]["ref"]
        assert g.act("type", ref=textbox, text="hello").ok
        assert live_session.eval("document.getElementById('q').value") == "hello"

        doc = g.extract()
        assert doc.ok and "article prose" in doc.data["text"]


def test_act_by_coordinate_escape_hatch(live_session):
    g = Gateway(live_session)
    with serve_pages({"p.html": _PAGE}) as d:
        g.session("open", url=f"file://{d}/p.html", wait_text="Head")
        # 坐标逃生舱：不经 Ref 也能点（Canvas/图标 UI 用得上）
        assert g.act("click", coordinate=(5, 5)).ok


def test_navigation_invalidates_the_snapshot(live_session):
    pages = {"p.html": _PAGE, "q.html": "<!doctype html><title>P2</title><h1>Second</h1>"}
    g = Gateway(live_session)
    with serve_pages(pages) as d:
        g.session("open", url=f"file://{d}/p.html", wait_text="Head")
        ref = g.observe("snapshot").data["refs"][0]
        g.session("open", url=f"file://{d}/q.html", wait_text="Second")   # 换页，快照作废
        r = g.act("click", ref=ref)
        assert not r.ok and "no snapshot" in r.error, "换页后旧 Ref 不能再用"
