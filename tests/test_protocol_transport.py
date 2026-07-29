from __future__ import annotations

import json
import threading
import time

import pytest
import websocket

from sleight.core import protocol
from sleight.core.errors import ConnectionError as SleightConnectionError
from sleight.core.errors import LeaseLost, ProtocolError
from sleight.core.errors import TimeoutError as SleightTimeout
from sleight.core.transport import Transport

# --------------------------------------------------------------------------- #
# protocol
# --------------------------------------------------------------------------- #


def test_encode_omits_empty_params():
    assert json.loads(protocol.encode(1, "Page.enable")) == {"id": 1, "method": "Page.enable"}
    assert json.loads(protocol.encode(2, "X", {"a": 1}, "sid")) == {
        "id": 2, "method": "X", "params": {"a": 1}, "sessionId": "sid"
    }


def test_decode_response_and_event():
    r = protocol.decode('{"id":7,"result":{"ok":true}}')
    assert isinstance(r, protocol.Response) and r.unwrap() == {"ok": True}

    e = protocol.decode('{"method":"Page.loadEventFired","params":{"timestamp":1}}')
    assert isinstance(e, protocol.Event) and e.method == "Page.loadEventFired"


def test_decode_keeps_error_on_the_response():
    """error 不在 decode 里抛 —— flush() 需要靠 id 定位是哪条命令失败了。"""
    r = protocol.decode('{"id":9,"error":{"code":-32000,"message":"nope"}}')
    assert isinstance(r, protocol.Response)
    assert r.error is not None and r.id == 9
    with pytest.raises(ProtocolError, match="nope"):
        r.unwrap("Some.method")


def test_decode_rejects_garbage():
    with pytest.raises(ProtocolError):
        protocol.decode("not json")
    with pytest.raises(ProtocolError):
        protocol.decode('{"neither":1}')


# --------------------------------------------------------------------------- #
# transport（fake socket）
# --------------------------------------------------------------------------- #


class FakeWS:
    """按 id 自动回响应；也能预置事件。"""

    def __init__(
        self, *, fail_ids: set[int] | None = None, silent: set[str] | None = None
    ) -> None:
        self.sent: list[dict] = []
        self.inbox: list[str] = []
        self.fail_ids = fail_ids or set()
        self.silent = silent or set()      # 这些方法故意不回包，模拟丢响应
        self.closed = False
        self._timeout = 30.0

    def send(self, frame: str) -> None:
        msg = json.loads(frame)
        self.sent.append(msg)
        if msg["method"] in self.silent:
            return
        if msg["id"] in self.fail_ids:
            self.inbox.append(
                json.dumps({"id": msg["id"], "error": {"code": -1, "message": "boom"}})
            )
        else:
            self.inbox.append(json.dumps({"id": msg["id"], "result": {"echo": msg["method"]}}))

    def recv(self) -> str:
        if not self.inbox:
            raise websocket.WebSocketTimeoutException("empty")
        return self.inbox.pop(0)

    def settimeout(self, t: float) -> None:
        self._timeout = t

    def close(self) -> None:
        self.closed = True

    def push_event(
        self, method: str, params: dict | None = None, session_id: str | None = None
    ) -> None:
        msg: dict = {"method": method, "params": params or {}}
        if session_id:
            msg["sessionId"] = session_id
        self.inbox.append(json.dumps(msg))

    def drop(self, msg_id: int) -> None:
        """扔掉某条响应，模拟浏览器丢了一次回包。"""
        self.inbox = [f for f in self.inbox if json.loads(f).get("id") != msg_id]


def test_call_round_trip():
    ws = FakeWS()
    t = Transport(ws)
    assert t.call("Page.enable") == {"echo": "Page.enable"}


def test_send_no_wait_does_not_block_and_flush_is_the_barrier():
    """拟人轨迹的命脉：N 个点只有一次等待，不是 N 次 RTT。"""
    ws = FakeWS()
    t = Transport(ws)
    for i in range(60):
        t.send_no_wait("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": i, "y": i})
    assert len(ws.sent) == 60
    assert len(t._inflight) == 60          # 一条响应都还没收
    t.flush()
    assert not t._inflight


def test_flush_surfaces_the_failing_command():
    ws = FakeWS(fail_ids={3})
    t = Transport(ws)
    t.send_no_wait("A")
    t.send_no_wait("B")
    t.send_no_wait("C")          # id=3
    t.send_no_wait("D")
    with pytest.raises(ProtocolError) as exc:
        t.flush()
    assert exc.value.method == "C"


def test_events_are_buffered_not_mistaken_for_responses():
    ws = FakeWS()
    t = Transport(ws)
    ws.push_event("Page.lifecycleEvent", {"name": "load"})
    ws.push_event("Network.requestWillBeSent", {"requestId": "r1"})
    t.call("Page.enable")
    events = list(t.drain_events())
    assert [e.method for e in events] == [
        "Page.lifecycleEvent", "Network.requestWillBeSent"
    ]


def test_events_are_bucketed_per_session():
    """两个 Session 共用一条 Transport 时不能互相吃掉事件。

    共用一个队列的后果：先 drain 的 Session 把别人的事件一并弹走，而
    Session._handle 只认自己的 sessionId 于是把它丢了 —— 对方永远等不到自己的 load。
    """
    ws = FakeWS()
    t = Transport(ws)
    ws.push_event("Page.lifecycleEvent", {"name": "load"}, session_id="B")
    ws.push_event("Page.lifecycleEvent", {"name": "load"}, session_id="A")
    ws.push_event("Target.targetCrashed", {})            # 浏览器级，无 sessionId
    t.call("Page.enable", session_id="A")

    got_a = [e.session_id for e in t.drain_events("A")]
    assert got_a == ["A"], "A must not swallow B's events"

    got_b = [e.session_id for e in t.drain_events("B")]
    assert got_b == ["B"], "B's event survived A draining"

    assert [e.method for e in t.drain_events()] == ["Target.targetCrashed"]


def test_abandoned_id_does_not_wedge_every_later_flush():
    """一次丢响应不该把整个 Session 变成废的。

    超时的 id 若留在 _inflight 里，之后每一次 flush() 都要空等满一个 timeout；
    而 InputDriver 每个动作末尾都 flush 一次。
    """
    ws = FakeWS(silent={"Page.enable"})
    t = Transport(ws)

    with pytest.raises(SleightTimeout):
        t.call("Page.enable", timeout=0.05)

    assert not t._inflight, "the abandoned id must not linger in _inflight"

    # 后续操作应当照常，flush 不再空等满一个 timeout
    assert t.call("Runtime.evaluate") == {"echo": "Runtime.evaluate"}
    started = time.monotonic()
    t.flush(timeout=5.0)
    assert time.monotonic() - started < 0.5, "flush blocked on the abandoned id"


def test_severed_transport_reports_lease_lost_not_connection_error():
    """租约失效掐断连接后，调用方看到的错误必须和"隧道抖了"区分得开。"""
    t = Transport(FakeWS())
    t.close(severed=True)
    assert t.severed
    with pytest.raises(LeaseLost):
        t.send_no_wait("Page.enable")

    ordinary = Transport(FakeWS())
    ordinary.close()
    assert not ordinary.severed
    with pytest.raises(SleightConnectionError):
        ordinary.send_no_wait("Page.enable")


def test_transport_rejects_cross_thread_use():
    """静默错帧极难排查，宁可直接报错。"""
    t = Transport(FakeWS())
    boom: list[BaseException] = []

    def other() -> None:
        try:
            t.send_no_wait("Page.enable")
        except BaseException as exc:
            boom.append(exc)

    th = threading.Thread(target=other)
    th.start()
    th.join()
    assert boom and isinstance(boom[0], RuntimeError)
    assert "not thread-safe" in str(boom[0])


def test_close_is_idempotent_and_allowed_from_any_thread():
    """租约失效时续租线程要能立刻掐断连接。"""
    ws = FakeWS()
    t = Transport(ws)
    th = threading.Thread(target=t.close)
    th.start()
    th.join()
    assert ws.closed and t.closed
    t.close()
