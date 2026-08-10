"""真浏览器下的跨源 OOPIF：读入 + 点进另一个进程里的 iframe。

跨源 iframe 是独立进程、独立 CDP target，主 session 的执行上下文够不着它。同一个 HTTP
服务同时用两个 loopback host 访问（父页走 127.0.0.1、子页走 localhost），在站点隔离下就
造出真正的 OOPIF —— fake transport 造不出"另一个进程"。没有浏览器时整个模块 skip。

这正是 README 那个例子的硬骨头：DataDome 之类的验证码就在跨源 iframe 里。
"""

from __future__ import annotations

import pytest

from sleight.core.types import Selector

from .conftest import serve_http

pytestmark = pytest.mark.integration


def _pages(port: int) -> dict[str, str]:
    """同一个服务上的两个页面：父页用 127.0.0.1 打开，iframe 指向 localhost（跨源）。"""
    return {
        "child.html": (
            "<!doctype html><title>C</title>"
            "<button id=b style='margin:50px 30px;padding:16px' "
            "onclick='window.__hit = event.isTrusted'>slider</button>"
        ),
        "parent.html": (
            "<!doctype html><title>P</title><h1>Parent</h1><div style='height:20px'></div>"
            f"<iframe id=cap src='http://localhost:{port}/child.html' "
            "style='width:360px;height:220px;border:1px solid #000'></iframe>"
        ),
    }


def _load_with_oopif(live_session, port):
    live_session.open(f"http://127.0.0.1:{port}/parent.html", wait=Selector("#cap"))
    for _ in range(25):
        live_session.pump_events(0.2)
        oopifs = [f for f in live_session.frames() if not f.is_main and not f.reachable]
        if oopifs:
            return oopifs
    return []


def test_oopif_is_enumerated_and_read_via_a_sub_session(live_session):
    with serve_http(_pages) as port:
        oopifs = _load_with_oopif(live_session, port)
        assert oopifs, "应枚举到跨源 OOPIF"
        assert any(o.url.endswith("child.html") for o in oopifs)

        view = live_session.frame("child.html")
        assert view.text() == "slider", "应通过 attach 出的子 session 读到 OOPIF 内容"
        assert view.count("#b") == 1


def test_click_into_a_cross_origin_oopif_lands_trusted(live_session):
    with serve_http(_pages) as port:
        _load_with_oopif(live_session, port)

        target = live_session.frame_element("#cap", "#b")
        assert target.exists()
        live_session.click(target)               # 顶层 session 发输入 + 跨 frame 坐标换算
        landed = live_session.frame("child.html").eval("window.__hit")
        assert landed is True, "跨源 iframe 里的按钮没有收到可信点击"
