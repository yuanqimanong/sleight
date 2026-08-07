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

import base64
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

from sleight.core.errors import (
    ElementError,
    ProtocolError,
    SleightError,
    TimeoutError,
)
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
        self.quiet: list[str] = []
        self.urgent: list = []
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

    def send_no_wait(self, method, params=None, *, session_id=None, quiet=False):
        self.sent.append((method, params or {}))
        if quiet:
            self.quiet.append(method)
        return len(self.sent)

    def flush(self, **kw) -> None:
        self.flushes += 1

    @contextmanager
    def urgent_events(self, handler):
        """读到就立刻处理的 handler。真 Transport 在 recv 的调用栈里调它。"""
        self.urgent.append(handler)
        try:
            yield handler
        finally:
            self.urgent.remove(handler)

    def deliver(self, event) -> bool:
        """模拟"事件刚从 socket 读出来" —— urgent 优先，否则进缓冲。"""
        for handler in self.urgent:
            if handler(event):
                return True
        self._events.append(event)
        return False

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
        self.deliver(Event(method, params, session_id))

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


# --------------------------------------------------------------------------- #
# 清理类 API
# --------------------------------------------------------------------------- #

ORIGIN = "https://www.example.com"


def clearing(cookies_before, cookies_after, *, usage=(4096, 0)):
    """建一个 Session，让 getCookies 第一次和第二次返回不同的结果。"""
    s, t = build()
    calls = iter([cookies_before, cookies_after])
    t.results["Network.getCookies"] = lambda p: {"cookies": next(calls, cookies_after)}
    sizes = iter(usage)
    t.results["Storage.getUsageAndQuota"] = lambda p: {"usage": next(sizes, usage[-1])}
    return s, t


def test_clear_site_data_clears_more_than_cookies_by_default():
    """只清 cookie 会被 localStorage / indexedDB 里的副本立刻还原。"""
    s, t = clearing([], [])
    s.clear_site_data(ORIGIN)

    params = next(p for m, p, _ in t.calls if m == "Storage.clearDataForOrigin")
    assert set(params["storageTypes"].split(",")) == {
        "cookies", "local_storage", "indexeddb", "cache_storage", "service_workers"
    }


def test_clear_site_data_is_scoped_to_the_origin():
    """全局清理会把别的站点的登录态一起端掉 —— 依赖插件的场景下这是致命的。"""
    s, t = clearing([], [])
    s.clear_site_data(ORIGIN)
    assert "Network.clearBrowserCookies" not in t.methods()
    assert next(p for m, p, _ in t.calls if m == "Storage.clearDataForOrigin")["origin"] == ORIGIN


def test_the_report_names_the_cookies_that_actually_went_away():
    """「datadome 到底清没清掉」是排查时最值钱的一条信息，而 CDP 什么都不返回。"""
    s, _ = clearing(
        [{"name": "datadome"}, {"name": "session"}],
        [{"name": "session"}],
    )
    report = s.clear_site_data(ORIGIN)
    assert report.cookies == ("datadome",)
    assert report.usage_before == 4096 and report.usage_after == 0
    assert report


def test_a_clear_that_removed_nothing_is_falsy():
    """空清理是最常见的"看起来成功了"。"""
    s, _ = clearing([{"name": "datadome"}], [{"name": "datadome"}], usage=(4096, 4096))
    assert not s.clear_site_data(ORIGIN)


def test_a_url_with_a_path_is_normalised_to_its_origin():
    """带 path 传进 clearDataForOrigin 既不报错也不生效。"""
    s, t = clearing([], [])
    report = s.clear_site_data("https://www.example.com/news/article?x=1#frag")
    assert report.origin == ORIGIN
    assert next(p for m, p, _ in t.calls if m == "Storage.clearDataForOrigin")["origin"] == ORIGIN


def test_an_origin_without_a_scheme_is_refused():
    s, _ = clearing([], [])
    with pytest.raises(ValueError, match="scheme and a host"):
        s.clear_site_data("www.example.com")


def test_a_misspelled_storage_type_is_refused_not_ignored():
    """CDP 收的是逗号分隔的字符串，拼错一个词不报错、静默不生效。"""
    s, _ = clearing([], [])
    with pytest.raises(ValueError):
        s.clear_site_data(ORIGIN, types=["localstorage"])


def test_selecting_types_narrows_what_is_sent():
    s, t = clearing([], [])
    s.clear_site_data(ORIGIN, types=["cookies"])
    assert next(
        p for m, p, _ in t.calls if m == "Storage.clearDataForOrigin"
    )["storageTypes"] == "cookies"


def test_an_empty_type_list_is_a_mistake():
    s, _ = clearing([], [])
    with pytest.raises(ValueError, match="at least one"):
        s.clear_site_data(ORIGIN, types=[])


def test_unmeasurable_cookies_report_none_not_empty():
    """"没测量"和"一个都没有"是两回事 —— 后者会让人以为清干净了。"""
    s, t = build()
    t.results["Network.getCookies"] = ProtocolError("Network domain is not enabled")
    t.results["Storage.getUsageAndQuota"] = {"usage": 0}
    assert s.clear_site_data(ORIGIN).cookies is None


def test_cookies_can_be_scoped_to_urls_without_navigating():
    s, t = build()
    t.results["Network.getCookies"] = lambda p: {"cookies": [{"name": "a", "urls": p.get("urls")}]}
    s.cookies(urls=[ORIGIN])
    assert next(p for m, p, _ in t.calls if m == "Network.getCookies")["urls"] == [ORIGIN]


def test_cookies_without_urls_stays_page_scoped():
    s, t = build()
    s.cookies()
    assert next(p for m, p, _ in t.calls if m == "Network.getCookies") == {}


def test_clear_browser_data_is_the_explicit_nuke():
    s, t = build()
    s.clear_browser_data()
    assert "Network.clearBrowserCookies" in t.methods()
    assert "Network.clearBrowserCache" in t.methods()


def test_clear_browser_data_can_spare_the_cache():
    s, t = build()
    s.clear_browser_data(cache=False)
    assert "Network.clearBrowserCookies" in t.methods()
    assert "Network.clearBrowserCache" not in t.methods()


# --------------------------------------------------------------------------- #
# exit_ip
# --------------------------------------------------------------------------- #


def ip_session(replies):
    """让 fetch(...) 依次返回 replies 里的东西。异常实例会被抛出来。"""
    answers = iter(replies)

    def evaluate(params):
        value = next(answers, None)
        if isinstance(value, BaseException):
            raise value
        return {"result": {"value": value}}

    s, t = build()
    t.results["Runtime.evaluate"] = evaluate
    return s, t


def test_exit_ip_returns_the_address():
    s, _ = ip_session(["203.0.113.7\n"])
    assert s.exit_ip() == "203.0.113.7"


def test_exit_ip_moves_on_when_an_endpoint_is_flaky():
    s, t = ip_session([ProtocolError("net::ERR_FAILED"), "203.0.113.7"])
    assert s.exit_ip() == "203.0.113.7"
    assert len(t.evaluates()) == 2


def test_exit_ip_rejects_things_that_only_look_like_an_ip():
    """页面上的 2026.08.06 能匹配大多数手写 IPv4 正则 —— 所以这里不用正则。"""
    s, _ = ip_session(["2026.08.06", "12:34:56", "<html>nope</html>"])
    with pytest.raises(SleightError) as exc:
        s.exit_ip(endpoints=["a://x", "b://x", "c://x"])
    assert "2026.08.06" in str(exc.value), "the error must show what each endpoint gave"


def test_exit_ip_accepts_ipv6():
    s, _ = ip_session(["2001:db8::1"])
    assert s.exit_ip() == "2001:db8::1"


def test_exit_ip_says_which_endpoints_it_tried():
    s, _ = ip_session([None, None])
    with pytest.raises(SleightError, match="ipify"):
        s.exit_ip(endpoints=["https://api.ipify.org", "https://icanhazip.com"])


# --------------------------------------------------------------------------- #
# 元素截图
# --------------------------------------------------------------------------- #

PNG = base64.b64encode(b"\x89PNG fake").decode()


def shooting(*, box=(40.0, 60.0, 120.0, 30.0), scroll=(0, 0)):
    x, y, w, h = box

    def evaluate(expr):
        if "window.scrollX" in expr:
            return list(scroll)
        if "getBoundingClientRect" in expr:
            return {"x": x, "y": y, "w": w, "h": h}
        if "innerHeight" in expr or "in_viewport" in expr:
            return True
        return True

    s, t = build(evaluate=evaluate)
    t.results["Page.captureScreenshot"] = {"data": PNG}
    return s, t


def test_whole_page_screenshot_sends_no_clip():
    s, t = shooting()
    s.screenshot()
    assert "clip" not in next(p for m, p, _ in t.calls if m == "Page.captureScreenshot")


def test_element_screenshot_clips_to_the_box():
    s, t = shooting()
    assert s.screenshot(target="#captcha") == b"\x89PNG fake"
    clip = next(p for m, p, _ in t.calls if m == "Page.captureScreenshot")["clip"]
    assert (clip["x"], clip["y"], clip["width"], clip["height"]) == (40.0, 60.0, 120.0, 30.0)


def test_the_clip_is_in_page_coordinates_not_viewport_ones():
    """getBoundingClientRect 是 viewport 坐标，clip 是页面坐标 —— 差一个滚动偏移。

    忘了加的话，页面没滚动时完全正常，滚过之后截出来的图整体偏移。
    """
    s, t = shooting(scroll=(0, 800))
    s.screenshot(target="#captcha")
    clip = next(p for m, p, _ in t.calls if m == "Page.captureScreenshot")["clip"]
    assert clip["y"] == 860.0, "scroll offset was not added to the clip"
    assert clip["x"] == 40.0


def test_element_screenshot_writes_the_file_too(tmp_path):
    s, _ = shooting()
    out = tmp_path / "captcha.png"
    s.screenshot(str(out), target="#captcha")
    assert out.read_bytes() == b"\x89PNG fake"


def test_a_zero_size_element_cannot_be_screenshotted():
    s, _ = shooting(box=(10.0, 10.0, 0.0, 0.0))
    with pytest.raises(ElementError, match="zero size"):
        s.screenshot(target="#hidden")


def test_element_screenshot_via_the_element_object():
    s, t = shooting()
    s.require("#captcha").screenshot()
    assert "clip" in next(p for m, p, _ in t.calls if m == "Page.captureScreenshot")


# --------------------------------------------------------------------------- #
# 请求屏蔽
# --------------------------------------------------------------------------- #


def paused(request_id, kind, url="https://cdn.example.com/a.png"):
    return {"requestId": request_id, "resourceType": kind, "request": {"url": url}}


def test_block_needs_something_to_block():
    s, _ = build()
    with pytest.raises(ValueError, match="no-op"), s.block():
        pass


def test_block_refuses_a_misspelled_resource_type():
    """大小写写错的后果是静默什么都不拦 —— 比报错难查得多。"""
    s, _ = build()
    with pytest.raises(ValueError, match="StyleSheet"), s.block(types=["StyleSheet"]):
        pass


def test_blocked_types_are_failed_and_everything_else_continues():
    s, t = build()
    with s.block(types=["Image"]) as stats:
        t.emit("Fetch.requestPaused", paused("R1", "Image"))
        t.emit("Fetch.requestPaused", paused("R2", "Document", "https://example.com/"))

    sent = dict(t.sent)
    assert sent["Fetch.failRequest"]["requestId"] == "R1"
    assert sent["Fetch.failRequest"]["errorReason"] == "BlockedByClient"
    assert sent["Fetch.continueRequest"] == {"requestId": "R2"}
    assert stats.by_type == {"Image": 1} and stats.allowed == 1


def test_url_patterns_block_independently_of_type():
    s, t = build()
    with s.block(url_patterns=["*://ads.*"]) as stats:
        t.emit("Fetch.requestPaused", paused("R1", "Script", "https://ads.example.com/x.js"))
        t.emit("Fetch.requestPaused", paused("R2", "Script", "https://example.com/app.js"))
    assert stats.blocked == 1 and stats.allowed == 1


def test_answers_are_fire_and_forget_and_allowed_to_fail():
    """每个请求一次 RTT 会把"省流量"变成"更慢"。而请求可能在我们回应前就被取消了。"""
    s, t = build()
    with s.block(types=["Image"]):
        t.emit("Fetch.requestPaused", paused("R1", "Image"))
    assert not [m for m, _, _ in t.calls if m.startswith("Fetch.fail")], "used call(), not send"
    assert t.quiet, "a stale interception id would blow up an unrelated flush"


def test_fetch_is_always_disabled_on_the_way_out():
    """disable 会放行还挂着的请求 —— 中途抛异常也不能把页面卡死。"""
    s, t = build()
    with pytest.raises(RuntimeError), s.block(types=["Image"]):
        raise RuntimeError("boom")
    assert "Fetch.disable" in t.methods()


def test_the_observer_is_removed_afterwards():
    s, t = build()
    with s.block(types=["Image"]):
        pass
    t.emit("Fetch.requestPaused", paused("R9", "Image"))
    s.drain()
    assert not t.sent, "still answering requestPaused after the block() block ended"


def test_paused_requests_are_answered_from_inside_a_blocking_call():
    """真机上撞过的死锁，这条是它的回归测试。

    普通事件在 ``call()`` 期间只进缓冲、等调用返回才派发。而 ``Page.navigate``
    的响应要等文档请求放行 —— 双方互等，``open()`` 死等到超时。所以裁决必须走
    ``urgent_events``：读到就回，不进缓冲。
    """
    s, t = build()

    def navigate_pauses_the_document(params):
        # 模拟：navigate 的响应回来之前，文档请求先被暂停
        t.emit("Fetch.requestPaused", paused("DOC", "Document", params["url"]))
        return {"frameId": "F1", "loaderId": "L1"}

    t.results["Page.navigate"] = navigate_pauses_the_document

    with s.block(types=["Image"]) as stats:
        t.call("Page.navigate", {"url": "https://example.com/"}, session_id=SID)

    assert dict(t.sent).get("Fetch.continueRequest") == {"requestId": "DOC"}, (
        "the document request was never released — this is the deadlock"
    )
    assert stats.allowed == 1


def test_urgent_handlers_that_decline_leave_the_event_in_the_buffer():
    """认领了才吞掉。别的事件必须照常喂给 Session 的状态机和观察者。"""
    s, t = build()
    seen: list[str] = []
    with s.block(types=["Image"]), s.observe_events(lambda ev: seen.append(ev.method)):
        t.emit("Fetch.requestPaused", paused("R1", "Image"))
        t.emit("Page.lifecycleEvent", {"name": "load", "loaderId": "L1"})
        s.drain()
    assert seen == ["Page.lifecycleEvent"], f"got {seen}"


# --------------------------------------------------------------------------- #
# reload / back / forward
# --------------------------------------------------------------------------- #

HISTORY = {
    "currentIndex": 1,
    "entries": [
        {"id": 10, "url": "https://a.example/"},
        {"id": 11, "url": "https://b.example/"},
        {"id": 12, "url": "https://b.example/#two"},
    ],
}


def committing(t: FakeTransport, method: str, loader: str = "L2") -> None:
    """让某条命令在响应之后产生一次真正的提交（frameNavigated + 生命周期）。"""
    def handler(params):
        t.emit("Page.frameNavigated", {"frame": {"id": "F1", "loaderId": loader}})
        t.emit("Page.lifecycleEvent", {"name": "DOMContentLoaded", "loaderId": loader})
        t.emit("Page.lifecycleEvent", {"name": "load", "loaderId": loader})
        return {}
    t.results[method] = handler


def test_reload_uses_page_reload_not_a_navigate():
    """navigate 到同一个 URL 会命中缓存 —— 语义和刷新不是一回事。"""
    s, t = build()
    committing(t, "Page.reload")
    s.reload()
    assert "Page.reload" in t.methods()
    assert "Page.navigate" not in t.methods()


def test_reload_can_bypass_the_cache():
    s, t = build()
    committing(t, "Page.reload")
    s.reload(ignore_cache=True)
    assert next(p for m, p, _ in t.calls if m == "Page.reload") == {"ignoreCache": True}


def land_on(s, t: FakeTransport, url: str, loader: str = "L1") -> None:
    """真的导航过去一次，让后续的 reload/back 有个像样的起点。"""
    navigating(t, loader)
    t.on_pump = lambda tr: tr.lifecycle("DOMContentLoaded", loader)
    s.open(url)
    t.on_pump = None


def test_reload_rebinds_the_navigation_epoch():
    """Page.reload **不返回 loaderId** —— 纪元只能从 frameNavigated 认。

    认晚一步，同一轮 drain 里紧跟着的 DOMContentLoaded 就会因为 loaderId 还是旧的
    而被丢掉，然后 wait 一路等到超时。
    """
    s, t = build()
    land_on(s, t, "https://b.example/")
    assert s._loader_id == "L1"

    committing(t, "Page.reload", loader="L2")
    s.reload()
    assert s._loader_id == "L2", "still on the old epoch"
    assert "DOMContentLoaded" in s._lifecycle, "the new epoch's events were dropped"


def test_a_subframe_navigation_does_not_steal_the_epoch():
    s, t = build()
    land_on(s, t, "https://b.example/")

    def handler(params):
        t.emit("Page.frameNavigated", {"frame": {"id": "IFRAME", "loaderId": "LX"}})
        t.emit("Page.frameNavigated", {"frame": {"id": "F1", "loaderId": "L2"}})
        t.emit("Page.lifecycleEvent", {"name": "DOMContentLoaded", "loaderId": "L2"})
        return {}
    t.results["Page.reload"] = handler

    s.reload()
    assert s._loader_id == "L2"


def test_back_and_forward_walk_the_history():
    s, t = build()
    t.results["Page.getNavigationHistory"] = HISTORY
    committing(t, "Page.navigateToHistoryEntry")

    s.back()
    assert next(p for m, p, _ in t.calls if m == "Page.navigateToHistoryEntry") == {"entryId": 10}

    t.calls.clear()
    s.forward()
    assert next(p for m, p, _ in t.calls if m == "Page.navigateToHistoryEntry") == {"entryId": 12}


def test_walking_off_the_end_of_the_history_says_so():
    s, t = build()
    t.results["Page.getNavigationHistory"] = HISTORY
    with pytest.raises(SleightError, match="cannot go back"):
        s.back(steps=5)
    with pytest.raises(SleightError, match="cannot go forward"):
        s.forward(steps=5)
    assert "Page.navigateToHistoryEntry" not in t.methods()


def test_zero_steps_is_a_mistake():
    s, t = build()
    t.results["Page.getNavigationHistory"] = HISTORY
    with pytest.raises(ValueError, match="at least 1"):
        s.back(steps=0)


def test_a_same_document_history_entry_does_not_wait_for_lifecycle_events():
    """只差 fragment 的两条历史不产生任何 lifecycle 事件 —— 等它就是等到超时。

    判定靠比较两条历史的 URL，而不是"等不到就当成同文档" —— 后者会把一次慢提交
    读成成功。
    """
    s, t = build()
    t.results["Page.getNavigationHistory"] = HISTORY
    t.results["Page.navigateToHistoryEntry"] = {}       # 什么事件都不发

    started = time.monotonic()
    s.forward(timeout=20)                                # 11 → 12，只差 #two
    assert time.monotonic() - started < 1.0, "waited for a lifecycle event that never comes"


def test_a_cross_document_history_entry_does_wait():
    s, t = build()
    t.results["Page.getNavigationHistory"] = HISTORY
    t.results["Page.navigateToHistoryEntry"] = {}       # 提交迟迟不来
    with pytest.raises(TimeoutError):
        s.back(timeout=0.5)                              # 11 → 10，是跨文档


def test_history_reports_where_we_are():
    s, t = build()
    t.results["Page.getNavigationHistory"] = HISTORY
    index, entries = s.history()
    assert index == 1
    assert [e["url"] for e in entries] == [
        "https://a.example/", "https://b.example/", "https://b.example/#two"
    ]


# --------------------------------------------------------------------------- #
# select_option / upload_file / set_viewport
# --------------------------------------------------------------------------- #


def selecting(reply):
    """把 select_option 拼出来的那段 JS 的返回值接管掉，并记下它长什么样。

    ``return true;`` 是 ``Element.exists()`` 的探针，得让它照常通过。
    """
    return build(evaluate=lambda expr: True if "return true;" in expr else reply)


def test_select_option_needs_exactly_one_of_value_or_label():
    s, _ = selecting({"value": "HK"})
    with pytest.raises(ValueError, match="exactly one"):
        s.select_option("#c")
    with pytest.raises(ValueError, match="exactly one"):
        s.select_option("#c", value="HK", label="香港")


def test_select_option_dispatches_input_and_change():
    """只改 el.value 的话，联动的二级下拉不刷新、校验不触发，而页面看着是对的。"""
    s, t = selecting({"value": "HK"})
    assert s.select_option("#c", value="HK") == "HK"

    js = " ".join(t.evaluates())
    assert "new Event('input', {bubbles: true})" in js
    assert "new Event('change', {bubbles: true})" in js
    assert "hit.selected = true" in js


def test_select_option_by_label_matches_the_trimmed_text():
    s, t = selecting({"value": "hk"})
    s.select_option("#c", label="中国香港")
    js = " ".join(t.evaluates())
    assert "o.textContent.trim()" in js
    assert '"\\u4e2d\\u56fd\\u9999\\u6e2f"' in js or "中国香港" in js


def test_select_option_on_something_that_is_not_a_select():
    s, _ = selecting({"error": "not a <select>, it is a <div>"})
    with pytest.raises(ElementError, match="not a <select>"):
        s.select_option("#c", value="HK")


def test_a_missing_option_lists_what_was_available():
    s, _ = selecting({"error": 'no option with value="XX"; available: ["HK","US"]'})
    with pytest.raises(ElementError, match=r'available: \["HK","US"\]'):
        s.select_option("#c", value="XX")


def test_select_option_on_a_vanished_element():
    """require() 到 _eval 之间元素被删掉 —— 窗口小但真实，别让它变成 KeyError。"""
    s, _ = selecting(None)
    with pytest.raises(ElementError, match="is gone"):
        s.select_option("#c", value="HK")


def uploading(landed):
    """objectId 走 by-reference 那条路，其余 evaluate 按表达式路由。"""
    s, t = build()

    def evaluate(p):
        if not p.get("returnByValue", True):
            return {"result": {"objectId": "OBJ-9"}}
        if "f.size" in p["expression"]:
            return {"result": {"value": landed}}
        return {"result": {"value": True}}

    t.results["Runtime.evaluate"] = evaluate
    return s, t


def test_upload_file_uses_the_dom_command_not_a_fake_change_event():
    """FileList 在 JS 里造不出来 —— 伪造的 change 事件里 files 是空的。"""
    s, t = uploading([["a.png", 12], ["b.png", 34]])
    s.upload_file("#avatar", "/data/a.png", "/data/b.png")

    params = next(p for m, p, _ in t.calls if m == "DOM.setFileInputFiles")
    assert params == {"files": ["/data/a.png", "/data/b.png"], "objectId": "OBJ-9"}
    assert ("Runtime.releaseObject", {"objectId": "OBJ-9"}, SID) in t.calls, (
        "the node stays pinned in memory"
    )


def test_a_path_that_does_not_exist_on_the_browser_host_is_caught():
    """Chrome 不校验路径 —— 它就给你一个 0 字节的 File，表单于是上传了个空文件。"""
    s, _ = uploading([["here.png", 0]])
    with pytest.raises(ElementError, match="0 bytes"):
        s.upload_file("#avatar", "/definitely/not/here.png")


def test_an_empty_file_can_be_uploaded_on_purpose():
    s, _ = uploading([["empty.txt", 0]])
    s.upload_file("#avatar", "/data/empty.txt", allow_empty=True)


def test_upload_file_releases_the_node_even_when_the_command_fails():
    s, t = uploading([])
    t.results["DOM.setFileInputFiles"] = ProtocolError("File not found")
    with pytest.raises(ProtocolError):
        s.upload_file("#avatar", "/nope.png")
    assert "Runtime.releaseObject" in t.methods()


def test_upload_file_with_no_paths_is_a_mistake():
    s, _ = build()
    with pytest.raises(ValueError, match="at least one path"):
        s.upload_file("#avatar")


def test_set_viewport_overrides_the_device_metrics():
    s, t = build()
    s.set_viewport(1280, 2400)
    assert next(p for m, p, _ in t.calls if m == "Emulation.setDeviceMetricsOverride") == {
        "width": 1280, "height": 2400, "deviceScaleFactor": 1.0, "mobile": False,
    }
    s.clear_viewport()
    assert "Emulation.clearDeviceMetricsOverride" in t.methods()


def test_a_zero_sized_viewport_is_refused():
    s, t = build()
    for w, h in ((0, 800), (1280, -1)):
        with pytest.raises(ValueError, match="positive"):
            s.set_viewport(w, h)
    assert "Emulation.setDeviceMetricsOverride" not in t.methods()


def test_a_redirect_during_reload_rebinds_to_the_final_document():
    """一次重定向就有**两条** frameNavigated，生命周期事件挂在后一个 loaderId 上。

    真机上撞过：``http://example.com/`` → ``https://…``，只认第一条的话一切看着都对，
    就是 DomReady 永远等不到。
    """
    s, t = build()
    land_on(s, t, "http://example.com/")

    def redirecting(params):
        t.emit("Page.frameNavigated", {"frame": {"id": "F1", "loaderId": "L-http"}})
        t.emit("Page.lifecycleEvent", {"name": "init", "loaderId": "L-http"})
        t.emit("Page.frameNavigated", {"frame": {"id": "F1", "loaderId": "L-https"}})
        t.emit("Page.lifecycleEvent", {"name": "DOMContentLoaded", "loaderId": "L-https"})
        t.emit("Page.lifecycleEvent", {"name": "load", "loaderId": "L-https"})
        return {}
    t.results["Page.reload"] = redirecting

    s.reload(timeout=5)                                  # 只认第一条的话这里会超时
    assert s._loader_id == "L-https"
    assert "DOMContentLoaded" in s._lifecycle


def test_a_redirect_that_lands_after_the_commit_loop_still_counts():
    """第二次提交可能在 wait() 里才到 —— 观察者必须一直挂到 wait 结束。"""
    s, t = build()
    land_on(s, t, "http://example.com/")

    def first_hop(params):
        t.emit("Page.frameNavigated", {"frame": {"id": "F1", "loaderId": "L-http"}})
        return {}
    t.results["Page.reload"] = first_hop
    # 第二跳等到 wait() 开始 pump 才到
    t.on_pump = lambda tr: (
        tr.emit("Page.frameNavigated", {"frame": {"id": "F1", "loaderId": "L-https"}}),
        tr.emit("Page.lifecycleEvent", {"name": "DOMContentLoaded", "loaderId": "L-https"}),
        setattr(tr, "on_pump", None),
    )

    s.reload(timeout=5)
    assert s._loader_id == "L-https"
