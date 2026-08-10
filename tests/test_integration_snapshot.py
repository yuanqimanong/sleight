"""真浏览器下的 Snapshot / Ref：抓无障碍树 → 按 Ref 点击/输入（isTrusted）→ 导航后失效。

Ref 解析、backendNodeId → 几何/objectId、以及"点 Ref 产生的是真事件"这些都得在真 Chromium
上验证——fake 造不出真实 AX 树和可信输入。没有浏览器时整个模块 skip。
"""

from __future__ import annotations

import pytest

from sleight.core.element import ElementLike
from sleight.core.errors import StaleRef
from sleight.core.types import Selector, Text

from .conftest import serve_pages

pytestmark = pytest.mark.integration

_PAGE = (
    "<!doctype html><title>T</title><h1>Head</h1>"
    "<button id=go onclick='window.__hit = event.isTrusted'>Go</button>"
    "<input id=q placeholder=search>"
)


def _ref_for_role(snapshot, role):
    for node in _walk(snapshot.root):
        if node.role == role and node.ref:
            return node.ref
    return None


def _walk(node):
    yield node
    for child in node.children:
        yield from _walk(child)


def test_snapshot_lists_interactables_with_refs(live_session):
    with serve_pages({"p.html": _PAGE}) as d:
        live_session.open(f"file://{d}/p.html", wait=Text("Head"))
        snap = live_session.snapshot()
        text = snap.text()
        assert 'button "Go"' in text
        assert "[e" in text, "可交互元素应带 Ref"
        assert _ref_for_role(snap, "button") is not None
        assert _ref_for_role(snap, "textbox") is not None


def test_click_via_ref_is_trusted(live_session):
    with serve_pages({"p.html": _PAGE}) as d:
        live_session.open(f"file://{d}/p.html", wait=Text("Head"))
        snap = live_session.snapshot()
        target = snap.ref(_ref_for_role(snap, "button"))
        assert isinstance(target, ElementLike)
        live_session.click(target)
        assert live_session.eval("window.__hit") is True


def test_type_via_ref(live_session):
    with serve_pages({"p.html": _PAGE}) as d:
        live_session.open(f"file://{d}/p.html", wait=Text("Head"))
        snap = live_session.snapshot()
        live_session.type(snap.ref(_ref_for_role(snap, "textbox")), "hello")
        assert live_session.eval("document.getElementById('q').value") == "hello"


_PARENT_WITH_IFRAME = {
    "child.html": (
        "<!doctype html><title>C</title>"
        "<button id=cb onclick='window.__hit = event.isTrusted'>ChildBtn</button>"
        "<input id=ci placeholder=childinput>"
    ),
    "parent.html": (
        "<!doctype html><title>P</title><h1>Parent</h1>"
        "<button id=pb>ParentBtn</button>"
        "<iframe id=cap src='child.html' style='width:320px;height:220px'></iframe>"
    ),
}


def test_snapshot_merges_same_origin_iframe_content(live_session):
    with serve_pages(_PARENT_WITH_IFRAME) as d:
        live_session.open(f"file://{d}/parent.html", wait=Text("Parent"))
        live_session.pump_events(0.5)
        snap = live_session.snapshot()

        # iframe 内的按钮/输入框直接出现在合并树里，带自己的 Ref
        assert _ref_for_role_named(snap, "button", "ChildBtn"), "iframe 内按钮应合并进来"
        assert _ref_for_role_named(snap, "textbox", "childinput")
        # cross_frame=False 时不合并
        flat = live_session.snapshot(cross_frame=False)
        assert _ref_for_role_named(flat, "button", "ChildBtn") is None


def test_click_and_type_iframe_internal_elements_via_merged_ref(live_session):
    with serve_pages(_PARENT_WITH_IFRAME) as d:
        live_session.open(f"file://{d}/parent.html", wait=Text("Parent"))
        live_session.pump_events(0.5)
        snap = live_session.snapshot()

        live_session.click(snap.ref(_ref_for_role_named(snap, "button", "ChildBtn")))
        # onclick 在子 frame 的 window 上置位，从子 frame 读回
        assert live_session.frame("child.html").eval("window.__hit") is True

        live_session.type(snap.ref(_ref_for_role_named(snap, "textbox", "childinput")), "typed")
        assert live_session.frame("child.html").eval(
            "document.getElementById('ci').value"
        ) == "typed"


def _ref_for_role_named(snapshot, role, name_sub):
    for node in _walk(snapshot.root):
        if node.ref and node.role == role and name_sub in node.name:
            return node.ref
    return None


def test_ref_is_stable_then_stale_after_navigation(live_session):
    pages = {
        "p.html": _PAGE,
        "q.html": "<!doctype html><title>P2</title><h1 id=done>Second</h1>",
    }
    with serve_pages(pages) as d:
        live_session.open(f"file://{d}/p.html", wait=Text("Head"))
        snap = live_session.snapshot()
        ref = _ref_for_role(snap, "button")
        assert _ref_for_role(live_session.snapshot(), "button") == ref, "同节点跨快照同一 Ref"

        live_session.open(f"file://{d}/q.html", wait=Selector("#done"))
        with pytest.raises(StaleRef):
            snap.ref(ref)
