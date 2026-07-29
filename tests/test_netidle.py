"""网络空闲状态机。用计数器而不是 requestId 集合的实现，会挂在下面大部分用例上。"""

from __future__ import annotations

import time

from sleight.core.netidle import NetworkIdleTracker
from sleight.core.protocol import Event


def ev(method: str, **params: object) -> Event:
    return Event(method=method, params=params)


def sent(rid: str, **kw: object) -> Event:
    return ev("Network.requestWillBeSent", requestId=rid, **kw)


def test_basic_in_flight_tracking():
    t = NetworkIdleTracker()
    t.feed(sent("r1"))
    assert not t.is_idle(0.0)
    t.feed(ev("Network.loadingFinished", requestId="r1"))
    assert t.is_idle(0.0)


def test_failed_request_does_not_wedge_forever():
    """计数器实现的第一个失效方式：loadingFailed 不减。"""
    t = NetworkIdleTracker()
    t.feed(sent("r1"))
    t.feed(ev("Network.loadingFailed", requestId="r1", errorText="net::ERR_FAILED"))
    assert t.is_idle(0.0)


def test_redirect_reuses_request_id_and_is_not_double_counted():
    """第二个失效方式：重定向复用同一个 requestId。"""
    t = NetworkIdleTracker()
    t.feed(sent("r1"))
    t.feed(sent("r1", redirectResponse={"status": 302}))     # 同一个 requestId
    t.feed(ev("Network.loadingFinished", requestId="r1"))
    assert t.is_idle(0.0), "a redirect must not leave a phantom request in flight"


def test_long_lived_connections_are_ignored():
    """第三个失效方式：WebSocket / EventSource 永不结束。"""
    t = NetworkIdleTracker()
    t.feed(sent("ws1", type="WebSocket"))
    t.feed(sent("es1", type="EventSource"))
    assert t.is_idle(0.0)


def test_long_lived_discovered_late_is_evicted():
    """requestWillBeSent 有时不带 type，responseReceived 补一刀。"""
    t = NetworkIdleTracker()
    t.feed(sent("x1"))
    assert not t.is_idle(0.0)
    t.feed(ev("Network.responseReceived", requestId="x1", type="EventSource"))
    assert t.is_idle(0.0)


def test_frame_filter():
    t = NetworkIdleTracker(frame_id="F1")
    t.feed(sent("other", frameId="F2"))
    assert t.is_idle(0.0)
    t.feed(sent("mine", frameId="F1"))
    assert not t.is_idle(0.0)


def test_reset_drops_previous_navigation_state():
    """第四个失效方式：跨导航的迟到事件。"""
    t = NetworkIdleTracker()
    t.feed(sent("stale"))
    t.reset(frame_id="F9")
    assert t.is_idle(0.0)
    assert t.frame_id == "F9"


def test_streaming_fetch_does_not_wedge_idle_forever():
    """分块 Fetch（长轮询 / SSE-over-fetch / gRPC-web）的 type 是 'Fetch'，
    只按 LONG_LIVED_TYPES 过滤拦不住，它会**永远不结束**。"""
    t = NetworkIdleTracker()
    t.feed(sent("stream", type="Fetch"))
    assert not t.is_idle(0.0)
    t.feed(ev("Network.responseReceived", requestId="stream", type="Fetch",
              response={"headers": {"Transfer-Encoding": "chunked"}}))
    assert t.is_idle(0.0)


def test_chunked_with_content_length_is_still_tracked():
    """有 Content-Length 就说明会结束，不该被误踢。"""
    t = NetworkIdleTracker()
    t.feed(sent("normal", type="Fetch"))
    t.feed(ev("Network.responseReceived", requestId="normal", type="Fetch",
              response={"headers": {"Transfer-Encoding": "chunked", "Content-Length": "512"}}))
    assert not t.is_idle(0.0)


def test_stale_requests_are_evicted_as_a_backstop():
    """识别不出来的流靠年龄兜底，否则 NetworkIdle 永远不满足。"""
    t = NetworkIdleTracker(stale_after=0.05)
    t.feed(sent("mystery"))
    assert not t.is_idle(0.0)
    time.sleep(0.08)
    assert t.is_idle(0.0)


def test_quiet_window():
    t = NetworkIdleTracker()
    t.feed(sent("r1"))
    assert t.quiet_for == 0.0
    t.feed(ev("Network.loadingFinished", requestId="r1"))
    assert not t.is_idle(5.0)      # 还没静默够 5 秒
    assert t.is_idle(0.0)
