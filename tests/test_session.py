"""Session 的导航、等待与生命周期。

用一个记录命令的假 Transport —— 不开浏览器。这一层真正要锁死的是三件在真浏览器上
很难复现、出了事又完全没痕迹的事：

1. **导航纪元**：上一次导航的迟到 lifecycle 事件会立刻满足这一次的等待，
   表现为"``wait`` 秒回但页面还是空的"。
2. **同文档导航**（hash 路由）不产生任何 lifecycle 事件，而且不能顺手把 ``_loader_id``
   清成 ``None`` —— 那等于把纪元过滤器**永久**解除武装。
3. **自建 target 的所有权**：自己开的要关，接管的只能 detach。
"""

from __future__ import annotations

import tempfile
import time
from pathlib import Path

import pytest

from sleight.core.errors import ProtocolError, SleightError, TimeoutError
from sleight.core.protocol import Event
from sleight.core.session import Session
from sleight.core.types import Gone, Load, NetworkIdle, Selector, Text

SID = "S1"
TID = "T1"


# --------------------------------------------------------------------------- #
# 假 Transport
# --------------------------------------------------------------------------- #


class FakeTransport:
    """按 method 路由的假 Transport，事件按 sessionId 分桶。

    ``pump`` 会真的睡一小会儿并触发 ``on_pump`` —— 让"事件晚一点才到"这种时序可测，
    也避免等待循环空转成忙等。
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict, str | None]] = []
        self.sent: list[tuple[str, dict]] = []
        self.flushes = 0
        self.pumps = 0
        self.closed = False
        self.results: dict[str, object] = {
            "Target.createTarget": {"targetId": TID},
            "Target.attachToTarget": {"sessionId": SID},
        }
        self.on_pump = None
        self._events: list[Event] = []

    # —— Transport 接口 ——

    def call(self, method, params=None, *, session_id=None, timeout=30):
        params = params or {}
        self.calls.append((method, params, session_id))
        handler = self.results.get(method)
        if isinstance(handler, BaseException):
            raise handler
        if callable(handler):
            return handler(params)
        return handler if handler is not None else {}

    def send_no_wait(self, method, params=None, *, session_id=None):
        self.sent.append((method, params or {}))
        return len(self.sent)

    def flush(self, **kw) -> None:
        self.flushes += 1

    def drain_events(self, session_id=None):
        keep, mine = [], []
        for ev in self._events:
            (mine if ev.session_id == session_id else keep).append(ev)
        self._events = keep
        return iter(mine)

    def pump(self, *, timeout: float) -> bool:
        self.pumps += 1
        if self.on_pump is not None:
            self.on_pump(self)
        if not self._events:
            time.sleep(min(timeout, 0.005))
        return bool(self._events)

    def close(self, **kw) -> None:
        self.closed = True

    # —— 测试辅助 ——

    def emit(self, method: str, params: dict, session_id: str | None = SID) -> None:
        self._events.append(Event(method, params, session_id))

    def lifecycle(self, name: str, loader_id: str) -> None:
        self.emit("Page.lifecycleEvent", {"name": name, "loaderId": loader_id})

    def evaluates(self) -> list[str]:
        return [p["expression"] for m, p, _ in self.calls if m == "Runtime.evaluate"]

    def methods(self) -> list[str]:
        return [m for m, _, _ in self.calls]


def evaluator(route):
    """把 ``expression -> value`` 的函数包成 ``Runtime.evaluate`` 的返回形状。"""
    return lambda params: {"result": {"value": route(params["expression"])}}


def build(*, evaluate=None, **kw) -> tuple[Session, FakeTransport]:
    t = FakeTransport()
    if evaluate is not None:
        t.results["Runtime.evaluate"] = evaluator(evaluate)
    return Session.create(t, **kw), t


def navigating(t: FakeTransport, loader_id: str | None, **extra) -> None:
    result = {"frameId": "F1", **extra}
    if loader_id is not None:
        result["loaderId"] = loader_id
    t.results["Page.navigate"] = result


# --------------------------------------------------------------------------- #
# 建立
# --------------------------------------------------------------------------- #


def test_create_opens_its_own_tab_and_attaches_flat():
    """默认**自建 target**。

    取 ``Target.getTargets()`` 里第一个 ``type=="page"`` 是错的：顺序没有业务语义，
    可能选中用户正在用的页面。``flatten: True`` 也必须给，否则得走老式的
    ``Target.sendMessageToTarget`` 嵌套封包。
    """
    _, t = build()
    assert ("Target.createTarget", {"url": "about:blank"}, None) in t.calls
    assert ("Target.attachToTarget", {"targetId": TID, "flatten": True}, None) in t.calls
    assert "Target.getTargets" not in t.methods()


def test_create_enables_the_domains_it_needs():
    _, t = build()
    for method in ("Page.enable", "Runtime.enable", "Page.setLifecycleEventsEnabled",
                   "Network.enable"):
        assert (method, {}, SID) in t.calls or method == "Page.setLifecycleEventsEnabled"
    assert ("Page.setLifecycleEventsEnabled", {"enabled": True}, SID) in t.calls


def test_network_tracking_can_be_turned_off():
    _, t = build(track_network=False)
    assert "Network.enable" not in t.methods()


def test_a_target_that_cannot_be_attached_is_not_left_behind():
    """建了 tab 却没接上，不能把它留在浏览器里泄漏。"""
    t = FakeTransport()
    t.results["Target.attachToTarget"] = SleightError("attach refused")
    with pytest.raises(SleightError):
        Session.create(t)
    assert ("Target.closeTarget", {"targetId": TID}, None) in t.calls


def test_attach_takes_over_an_existing_tab():
    t = FakeTransport()
    s = Session.attach(t, "OTHER")
    assert ("Target.attachToTarget", {"targetId": "OTHER", "flatten": True}, None) in t.calls
    assert s.owned_target is False


# --------------------------------------------------------------------------- #
# 导航纪元
# --------------------------------------------------------------------------- #


def test_open_waits_for_this_navigations_dom_ready():
    s, t = build()
    navigating(t, "L1")
    t.on_pump = lambda tr: tr.lifecycle("DOMContentLoaded", "L1")
    s.open("https://example.com", timeout=2)
    assert ("Page.navigate", {"url": "https://example.com"}, SID) in t.calls


def test_a_late_event_from_the_previous_navigation_is_dropped():
    """否则表现为"``wait`` 瞬间返回但页面还没加载" —— 后面每一步都在错误的页面上跑。"""
    s, t = build()
    navigating(t, "L2")
    # 等待过程中才到达，loaderId 却是上一轮的 —— 只能靠纪元过滤挡住
    t.on_pump = lambda tr: tr.lifecycle("DOMContentLoaded", "L1")
    with pytest.raises(TimeoutError):
        s.open("https://example.com", timeout=0.2)


def test_lifecycle_state_does_not_leak_across_navigations():
    s, t = build()
    navigating(t, "L1")
    t.on_pump = lambda tr: tr.lifecycle("DOMContentLoaded", "L1")
    s.open("https://a.example", timeout=2)

    navigating(t, "L2")                        # 第二次导航，什么事件都不来
    t.on_pump = None
    with pytest.raises(TimeoutError):
        s.open("https://b.example", timeout=0.2)


def test_same_document_navigation_does_not_hang():
    """hash 路由不产生任何 lifecycle 事件，``Page.navigate`` 也不返回 loaderId
    （实测只有 ``frameId`` 和 ``isDownload``）。照常等就是一路等到超时。"""
    s, t = build()
    navigating(t, None, isDownload=False)
    s.open("https://example.com/#section", timeout=0.15)      # 不抛


def test_same_document_navigation_keeps_the_epoch_filter_armed():
    """**不能**把 ``_loader_id`` 置成 ``None``。

    ``None`` 在过滤器里的含义是"接受一切 loaderId" —— 一次 hash 跳转就把纪元过滤永久
    解除武装，之后每次真导航都会被上一轮的迟到事件立刻满足。
    """
    s, t = build()
    navigating(t, "L1")
    t.on_pump = lambda tr: tr.lifecycle("DOMContentLoaded", "L1")
    s.open("https://example.com", timeout=2)

    t.on_pump = None
    navigating(t, None)
    s.open("https://example.com/#section", timeout=0.15)

    # 纪元仍然只认 L1：别的 loaderId 的 load 事件不该被接受
    t.on_pump = lambda tr: tr.lifecycle("load", "STALE")
    with pytest.raises(TimeoutError):
        s.wait(Load(), timeout=0.2)


def test_same_document_navigation_still_honours_a_polled_condition():
    """``DomReady`` / ``Load`` 直接返回，但 ``Text()`` 这类轮询条件照常要等到。"""
    s, t = build(evaluate=lambda expr: "Section title" if "innerText" in expr else None)
    navigating(t, None)
    s.open("https://example.com/#section", wait=Text("Section title"), timeout=2)


def test_a_failed_navigation_is_reported_not_waited_on():
    s, t = build()
    t.results["Page.navigate"] = {"errorText": "net::ERR_NAME_NOT_RESOLVED"}
    with pytest.raises(SleightError, match="ERR_NAME_NOT_RESOLVED"):
        s.open("https://nope.invalid", timeout=0.15)


# --------------------------------------------------------------------------- #
# 等待条件
# --------------------------------------------------------------------------- #


def test_load_and_dom_ready_are_separate_conditions():
    s, t = build()
    navigating(t, "L1")
    t.on_pump = lambda tr: tr.lifecycle("DOMContentLoaded", "L1")
    s.open("https://example.com", timeout=2)          # DomReady 已满足

    t.on_pump = None
    with pytest.raises(TimeoutError):
        s.wait(Load(), timeout=0.2)                   # load 还没来

    t.on_pump = lambda tr: tr.lifecycle("load", "L1")
    s.wait(Load(), timeout=2)


def test_text_condition_polls_inner_text():
    state = {"body": "Loading…"}
    s, t = build(evaluate=lambda expr: state["body"] if "innerText" in expr else None)
    t.on_pump = lambda tr: state.update(body="Please Sign in below")
    s.wait(Text("Sign in"), timeout=2)


def test_selector_condition_counts_matches():
    state = {"n": 0}
    s, t = build(evaluate=lambda expr: state["n"] if "querySelectorAll" in expr else None)
    t.on_pump = lambda tr: state.update(n=1)
    s.wait(Selector("#app"), timeout=2)
    assert 'document.querySelectorAll("#app").length' in t.evaluates()


def test_gone_condition_is_the_inverse():
    state = {"n": 1}
    s, t = build(evaluate=lambda expr: state["n"] if "querySelectorAll" in expr else None)
    with pytest.raises(TimeoutError):
        s.wait(Gone(".spinner"), timeout=0.15)
    t.on_pump = lambda tr: state.update(n=0)
    s.wait(Gone(".spinner"), timeout=2)


def test_selectors_are_embedded_as_json_not_python_repr():
    """Python 的 repr 走 Python 的引号与转义规则 —— 反斜杠、引号、非 ASCII 会产出
    不合法或语义不同的 JS 字面量。"""
    s, t = build(evaluate=lambda expr: 1)
    s.wait(Selector('a[href="x"]'), timeout=2)
    assert 'document.querySelectorAll("a[href=\\"x\\"]").length' in t.evaluates()


def test_a_timeout_carries_the_last_value_it_saw():
    """带上最后一次求值结果，否则排查等待超时只能靠猜。"""
    s, _ = build(evaluate=lambda expr: 0 if "querySelectorAll" in expr else None)
    with pytest.raises(TimeoutError) as exc:
        s.wait(Selector("#app"), timeout=0.15)
    assert exc.value.last_value == 0
    assert "Selector('#app')" in str(exc.value)


def test_an_unknown_condition_is_rejected_loudly():
    class Bogus:
        kind = "telepathy"

    s, _ = build()
    with pytest.raises(ValueError, match="unknown wait condition"):
        s.wait(Bogus(), timeout=1)      # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# NetworkIdle
# --------------------------------------------------------------------------- #


def test_network_idle_waits_for_requests_started_after_attach():
    s, t = build()
    t.emit("Network.requestWillBeSent", {"requestId": "R1"})
    with pytest.raises(TimeoutError):
        s.wait(NetworkIdle(quiet=0.01), timeout=0.15)

    t.emit("Network.loadingFinished", {"requestId": "R1"})
    s.wait(NetworkIdle(quiet=0.01), timeout=2)


def test_a_failed_request_does_not_wedge_network_idle():
    """``loadingFailed`` 不减的话，一个失败的请求把空闲判定永久卡死。"""
    s, t = build()
    t.emit("Network.requestWillBeSent", {"requestId": "R1"})
    t.emit("Network.loadingFailed", {"requestId": "R1", "errorText": "net::ERR_ABORTED"})
    s.wait(NetworkIdle(quiet=0.01), timeout=2)


def test_a_websocket_never_blocks_network_idle():
    """长连接永不结束，计入就再也空闲不了。"""
    s, t = build()
    t.emit("Network.requestWillBeSent", {"requestId": "WS", "type": "WebSocket"})
    s.wait(NetworkIdle(quiet=0.01), timeout=2)


def test_a_new_navigation_resets_the_in_flight_set():
    """``reset()`` 必须在**排空旧事件之后**做。

    顺序反过来的话，导航响应回来之前压在缓冲里的旧文档请求会在 reset 之后被
    drain 原样塞回集合 —— 这批请求随着旧文档一起没了、永远不会 ``loadingFinished``，
    ``NetworkIdle`` 只能干等 ``STALE_AFTER`` 那 15 秒。
    """
    s, t = build()
    t.emit("Network.requestWillBeSent", {"requestId": "R1"})     # 上一页还在飞的请求
    navigating(t, "L1")
    t.on_pump = lambda tr: tr.lifecycle("DOMContentLoaded", "L1")
    s.open("https://example.com", timeout=2)
    s.wait(NetworkIdle(quiet=0.01), timeout=2)


# --------------------------------------------------------------------------- #
# 事件观察者与资源监听
# --------------------------------------------------------------------------- #


def test_observers_do_not_have_to_monkeypatch_the_private_handler():
    seen: list[str] = []
    s, t = build()
    with s.observe_events(lambda ev: seen.append(ev.method)):
        t.emit("Page.lifecycleEvent", {"name": "load", "loaderId": "L1"})
        s.drain()
    assert seen == ["Page.lifecycleEvent"]


def test_observers_are_removed_on_exit():
    seen: list[str] = []
    s, t = build()
    with s.observe_events(lambda ev: seen.append(ev.method)):
        pass
    t.emit("Network.requestWillBeSent", {"requestId": "1"})
    s.drain()
    assert seen == []


def test_the_sessions_own_state_machine_still_runs_alongside_observers():
    """观察者不能顶掉导航纪元和网络空闲 —— 那会让 wait() 永远超时。"""
    s, t = build()
    navigating(t, "L1")
    with s.observe_events(lambda ev: None):
        t.on_pump = lambda tr: tr.lifecycle("DOMContentLoaded", "L1")
        s.open("https://example.com", timeout=2)


def test_a_raising_observer_does_not_break_the_event_stream():
    """一个坏回调不该把整个会话搞停。"""
    good: list[str] = []
    s, t = build()

    def boom(ev):
        raise RuntimeError("observer is broken")

    with s.observe_events(boom), s.observe_events(lambda ev: good.append(ev.method)):
        t.emit("Network.requestWillBeSent", {"requestId": "1"})
        s.drain()
    assert good == ["Network.requestWillBeSent"]


def test_capture_resources_collects_and_notifies():
    s, t = build()
    announced: list[str] = []

    with s.capture_resources(
        types={"Script", "Stylesheet"}, on_discovered=lambda r: announced.append(r.url)
    ) as capture:
        t.emit("Network.requestWillBeSent",
               {"requestId": "1", "type": "Script", "request": {"url": "https://x/a.js"}})
        t.emit("Network.requestWillBeSent",
               {"requestId": "2", "type": "Image", "request": {"url": "https://x/a.png"}})
        t.emit("Network.requestWillBeSent",
               {"requestId": "3", "request": {"url": "https://x/a.css"}})
        t.emit("Network.responseReceived",
               {"requestId": "3", "type": "Stylesheet",
                "response": {"status": 200, "mimeType": "text/css"}})
        s.drain()

        assert announced == ["https://x/a.js", "https://x/a.css"]

    # 退出上下文之后 tracker 还能查
    assert capture.urls("Script") == ["https://x/a.js"]
    assert sorted(capture.by_type()) == ["Script", "Stylesheet"]


def test_capture_stops_at_the_end_of_the_block():
    s, t = build()
    with s.capture_resources() as capture:
        pass
    t.emit("Network.requestWillBeSent",
           {"requestId": "1", "type": "Script", "request": {"url": "https://x/late.js"}})
    s.drain()
    assert capture.snapshot() == []


def test_capture_needs_the_network_domain():
    """track_network=False 时静默抓不到东西比报错难查得多。"""
    s, _ = build(track_network=False)
    with pytest.raises(SleightError, match="track_network"), s.capture_resources():
        pass


def test_capture_rejects_a_misspelled_type():
    s, _ = build()
    with pytest.raises(ValueError, match="StyleSheet"), s.capture_resources(types={"StyleSheet"}):
        pass


def test_two_captures_can_run_at_once():
    """两套筛选条件互不干扰 —— 观察者是列表，不是单个补丁。"""
    s, t = build()
    with s.capture_resources(types={"Script"}) as js,          s.capture_resources(types={"Image"}) as img:
        t.emit("Network.requestWillBeSent",
               {"requestId": "1", "type": "Script", "request": {"url": "https://x/a.js"}})
        t.emit("Network.requestWillBeSent",
               {"requestId": "2", "type": "Image", "request": {"url": "https://x/a.png"}})
        s.drain()
    assert js.urls() == ["https://x/a.js"]
    assert img.urls() == ["https://x/a.png"]


def test_pump_events_drains_for_the_requested_duration():
    s, t = build()
    collected: list[str] = []

    def deliver(tr):
        tr.emit("Network.requestWillBeSent",
                {"requestId": str(len(collected)), "type": "Script",
                 "request": {"url": f"https://x/{len(collected)}.js"}})

    t.on_pump = deliver
    with s.capture_resources(types={"Script"}, on_discovered=lambda r: collected.append(r.url)):
        s.pump_events(0.2, tick=0.01)
    assert collected, "0.2 秒内应该收到事件"
    assert t.pumps > 1


def test_pump_events_with_no_duration_is_a_noop():
    s, t = build()
    s.pump_events(0)
    s.pump_events(-1)
    assert t.pumps == 0


# --------------------------------------------------------------------------- #
# 读取
# --------------------------------------------------------------------------- #


def test_reads_go_through_runtime_evaluate():
    """用 evaluate **读** DOM 是安全的（读不伪造事件）；写交互不行。"""
    values = {
        "document.documentElement.outerHTML": "<html>hi</html>",
        "document.title": "Example",
        "location.href": "https://example.com/",
    }
    s, _ = build(evaluate=lambda expr: values.get(expr))
    assert s.content() == "<html>hi</html>"
    assert s.title() == "Example"
    assert s.url() == "https://example.com/"


def test_reads_degrade_to_empty_strings_not_none():
    s, _ = build(evaluate=lambda expr: None)
    assert s.content() == "" and s.title() == "" and s.text() == ""


def test_eval_surfaces_a_js_exception():
    t = FakeTransport()
    t.results["Runtime.evaluate"] = {
        "exceptionDetails": {"exception": {"description": "ReferenceError: nope is not defined"}}
    }
    s = Session.create(t)
    with pytest.raises(ProtocolError, match="ReferenceError"):
        s.eval("nope()")


def test_viewport_falls_back_when_the_page_cannot_answer():
    s, _ = build(evaluate=lambda expr: [1920, 947] if "innerHeight" in expr else None)
    assert s.viewport() == (1920, 947)


def test_query_returns_none_when_nothing_matches():
    def route(expr: str):
        if "return true;" in expr:
            return None                       # el 不存在，_eval 短路成 null
        return 0

    s, _ = build(evaluate=route)
    assert s.query("#missing") is None
    assert s.query_all("#missing") == []


def test_query_all_yields_one_element_per_match():
    def route(expr: str):
        if "return true;" in expr:
            return True
        return 3 if "querySelectorAll" in expr else None

    s, _ = build(evaluate=route)
    found = s.query_all("li")
    assert [e.index for e in found] == [0, 1, 2]
    assert s.query("li") is not None


def test_screenshot_decodes_base64_and_can_write_a_file():
    t = FakeTransport()
    t.results["Page.captureScreenshot"] = {"data": "aGk="}       # "hi"
    s = Session.create(t)
    # 不用 pytest 的 tmp_path fixture：它要 scandir 一个跨会话共享的基目录，
    # 在这台机器上会撞 WinError 5
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder, "shot.png")
        assert s.screenshot(str(path)) == b"hi"
        assert path.read_bytes() == b"hi"


def test_cookies_come_from_the_network_domain():
    t = FakeTransport()
    t.results["Network.getCookies"] = {"cookies": [{"name": "sid"}]}
    s = Session.create(t)
    assert s.cookies() == [{"name": "sid"}]


# --------------------------------------------------------------------------- #
# 关闭
# --------------------------------------------------------------------------- #


def test_an_owned_tab_is_closed_not_just_detached():
    """断 WebSocket ≠ 关 tab —— 不 closeTarget 就是泄漏。"""
    s, t = build()
    s.close()
    assert ("Target.closeTarget", {"targetId": TID}, None) in t.calls
    assert "Target.detachFromTarget" not in t.methods()


def test_an_attached_tab_is_only_detached():
    """接管的是别人的页面，关掉它不是我们的权限。"""
    t = FakeTransport()
    s = Session.attach(t, "OTHER")
    s.close()
    assert ("Target.detachFromTarget", {"sessionId": SID}, None) in t.calls
    assert "Target.closeTarget" not in t.methods()


def test_close_is_idempotent():
    s, t = build()
    s.close()
    s.close()
    assert t.methods().count("Target.closeTarget") == 1
    assert s.closed


def test_close_swallows_a_dead_connection():
    """连接已经断了还去 closeTarget 必然失败 —— 但那不该盖住调用方原本的异常。"""
    s, t = build()
    t.results["Target.closeTarget"] = SleightError("connection already gone")
    s.close()
    assert s.closed


def test_context_manager_closes_on_exit():
    s, t = build()
    with s:
        pass
    assert ("Target.closeTarget", {"targetId": TID}, None) in t.calls
