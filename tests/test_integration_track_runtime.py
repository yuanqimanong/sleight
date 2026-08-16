"""真浏览器下的 ``track_runtime=False``：省掉 Runtime 事件流，但主 frame 那条路一点不少。

这个开关成立与否，只有真浏览器能证：``Runtime.evaluate`` 到底需不需要先
``Runtime.enable``（不需要 —— enable 打开的是**事件**，evaluate 是**命令**），
以及关掉之后 Chromium 是不是真的不再往这条 CDP session 上推
``consoleAPICalled`` / ``exceptionThrown`` / ``executionContextCreated``。
fake transport 只能证明我们没发那条 enable，证明不了浏览器那边的行为。

背景：使用方的 CDP WebSocket 走跨境 VPN 隧道（RTT ~200ms，且与 MySQL 共用链路），
对事件量极度敏感。``Network`` 关掉之后 ``Runtime`` 是剩下最大的一股。

跑法：装了 chromium 直接 `pytest tests/test_integration_track_runtime.py`；或
`SLEIGHT_TEST_CHROMIUM=/path/to/chrome pytest -m integration`。
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest

from sleight import connect
from sleight.core.errors import SleightError
from sleight.core.types import Selector, Text

from .conftest import serve_pages

pytestmark = pytest.mark.integration


#: 一个会不停往 console 写东西、还抛未捕获异常的页面 —— Runtime 域开着时事件源源不断，
#: 关掉之后应当一条都收不到。带一个同源 iframe，用来验证 snapshot 合并不受影响。
NOISY = (
    "<!doctype html><title>Noisy</title><h1>Noisy page</h1>"
    "<button id=b onclick='window.__hit = event.isTrusted'>go</button>"
    "<iframe name='box' src='child.html'></iframe>"
    "<script>"
    "  for (let i = 0; i < 40; i++) console.log('chatter', i, 'x'.repeat(200));"
    "  setTimeout(() => { throw new Error('unhandled, on purpose'); }, 0);"
    "</script>"
)
CHILD = "<!doctype html><title>C</title><h1 id=inner>Inside iframe</h1>"


@contextmanager
def _session(live_endpoint, **kw):
    with connect(live_endpoint, **kw) as sess:
        yield sess


def _runtime_events(session, directory) -> list[str]:
    """在 noisy 页上跑一次导航，收本次会话收到的 ``Runtime.*`` 事件方法名。"""
    seen: list[str] = []
    with session.observe_events(lambda ev: seen.append(ev.method)):
        session.open(f"file://{directory}/noisy.html", wait=Text("Noisy page"))
        session.pump_events(1.0)          # 给 console/exception 事件充足的到达时间
    return [m for m in seen if m.startswith("Runtime.")]


def test_runtime_events_stop_arriving_when_the_domain_is_off(live_endpoint):
    """开关的**收益**本身：同一个页面，开着收得到 Runtime 事件，关掉一条都没有。

    默认那半边同时也是"这个页面确实吵"的对照 —— 没有它，"关掉之后是 0"可能只是页面
    根本没产生事件。
    """
    with serve_pages({"noisy.html": NOISY, "child.html": CHILD}) as d:
        with _session(live_endpoint) as on:
            noisy = _runtime_events(on, d)
        with _session(live_endpoint, track_runtime=False) as off:
            quiet = _runtime_events(off, d)

    assert noisy, "Runtime 域开着时这个页面必须能收到 Runtime 事件，否则对照不成立"
    assert any(m == "Runtime.consoleAPICalled" for m in noisy)
    assert quiet == [], f"关掉 Runtime 域后不该再收到任何 Runtime 事件，实际 {set(quiet)}"


def test_reading_and_interacting_still_work_without_the_runtime_domain(live_endpoint):
    """整个改动成立的前提：``Runtime.evaluate`` 是命令，不需要 enable。"""
    with serve_pages({"noisy.html": NOISY, "child.html": CHILD}) as d, \
            _session(live_endpoint, track_runtime=False) as s:
        # 导航 + Page 域的等待条件（wait 靠 Page.lifecycleEvent，与 Runtime 无关）
        s.open(f"file://{d}/noisy.html", wait=Text("Noisy page"))

        assert s.eval("1+1") == 2
        assert s.eval("document.title") == "Noisy"
        assert "Noisy page" in s.content()
        assert s.query("#b") is not None
        assert s.query("#nope") is None
        assert "Noisy page" in s.text()
        assert s.query("#b").text() == "go"

        # 轮询型等待条件也走 eval
        s.wait(Selector("#b"), timeout=5)

        # 交互链完整：真实 isTrusted 事件
        s.click("#b")
        assert s.eval("window.__hit") is True


def test_snapshot_still_merges_a_same_origin_iframe_without_the_runtime_domain(live_endpoint):
    """同进程子 frame 走 ``Accessibility.getFullAXTree({frameId})`` + ``DOM``，
    不碰 Runtime 域 —— 关了 Runtime 也照样合并。这是 snapshot() 文档里的承诺。"""
    with serve_pages({"noisy.html": NOISY, "child.html": CHILD}) as d, \
            _session(live_endpoint, track_runtime=False) as s:
        s.open(f"file://{d}/noisy.html", wait=Text("Noisy page"))
        s.pump_events(0.5)
        text = s.snapshot().text()

    assert "Inside iframe" in text, f"同源 iframe 的内容应照常合并进快照，实际:\n{text}"


def test_snapshot_ref_click_still_works_without_the_runtime_domain(live_endpoint):
    """Ref 解析走 ``DOM.resolveNode`` → ``Runtime.callFunctionOn`` → ``Runtime.releaseObject``。

    这三条**全是命令**，不需要 ``Runtime.enable``。这条用例就是拿真浏览器把这句话钉死 ——
    整个 LLM 层（snapshot → ref → click）在关掉 Runtime 域之后照常能用。
    """
    with serve_pages({"noisy.html": NOISY, "child.html": CHILD}) as d, \
            _session(live_endpoint, track_runtime=False) as s:
        s.open(f"file://{d}/noisy.html", wait=Text("Noisy page"))
        snap = s.snapshot()
        ref = next(
            (n.ref for n in _walk(snap.root) if n.role == "button" and n.ref), None
        )
        assert ref is not None, f"快照里应有 button 的 Ref:\n{snap.text()}"
        s.click(snap.ref(ref))
        assert s.eval("window.__hit") is True


def _walk(node):
    yield node
    for child in node.children:
        yield from _walk(child)


def test_frame_apis_refuse_clearly_without_the_runtime_domain(live_endpoint):
    """frame 层整层建在 Runtime 域上，关掉之后必须显式报错，而不是返回一棵
    "全都不可达"的树 —— 后者会让调用方拿着一个读不了任何东西的半成品。"""
    with serve_pages({"noisy.html": NOISY, "child.html": CHILD}) as d, \
            _session(live_endpoint, track_runtime=False) as s:
        s.open(f"file://{d}/noisy.html", wait=Text("Noisy page"))

        with pytest.raises(SleightError, match="track_runtime=False"):
            s.frames()
        with pytest.raises(SleightError, match="track_runtime=False"):
            s.frame("box")
        with pytest.raises(SleightError, match="track_runtime=False"):
            s.frame_element("iframe", "#inner")


def test_default_is_unchanged(live_endpoint):
    """默认 ``track_runtime=True``：frame 层行为与开关引入前完全一致。"""
    with serve_pages({"noisy.html": NOISY, "child.html": CHILD}) as d, \
            _session(live_endpoint) as s:
        s.open(f"file://{d}/noisy.html", wait=Text("Noisy page"))
        s.pump_events(0.5)

        child = next(f for f in s.frames() if not f.is_main)
        assert child.name == "box"
        assert child.reachable
        assert "Inside iframe" in s.frame("box").text()
