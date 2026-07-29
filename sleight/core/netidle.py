"""网络空闲判定。

用 **requestId 集合**而不是计数器。计数器的四个失效方式：

1. ``loadingFailed`` 不减 —— 失败的请求把空闲永久卡死
2. 重定向复用同一个 requestId，会被重复计入
3. WebSocket / EventSource 这类长连接永不结束
4. 跨导航的迟到事件污染下一次等待

语义：**"attach 之后发起的请求都结束了"**，不是"页面全部加载完" —— attach 之前
已经在飞的请求 sleight 看不到。
"""

from __future__ import annotations

import time

from .protocol import Event

__all__ = ["LONG_LIVED_TYPES", "NetworkIdleTracker"]

#: 永不"结束"的资源类型，计入就再也空闲不了
LONG_LIVED_TYPES = frozenset({"WebSocket", "EventSource"})

#: 单个请求在飞多久之后不再计入空闲判定。
#:
#: 只按 type 过滤是不够的：分块的 Fetch / XHR —— 长轮询、聊天、SSE-over-fetch、
#: gRPC-web、任何 ReadableStream 响应 —— 的 type 是 ``Fetch``，会被正常计入，然后
#: **永远不结束**，``NetworkIdle`` 就再也不会满足。按年龄剔除是兜底。
STALE_AFTER = 15.0


class NetworkIdleTracker:
    """喂 CDP Network 事件，问它空闲了多久。

    :param frame_id: 只跟踪该 frame 的请求。None = 不过滤。
    :param stale_after: 在飞超过这么久的请求视为流式连接，不再阻塞空闲判定。
    """

    __slots__ = ("_last_change", "_started", "frame_id", "in_flight", "stale_after")

    def __init__(self, *, frame_id: str | None = None, stale_after: float = STALE_AFTER) -> None:
        self.in_flight: set[str] = set()
        self.frame_id = frame_id
        self.stale_after = stale_after
        self._started: dict[str, float] = {}
        self._last_change = time.monotonic()

    # ------------------------------------------------------------------ #

    def feed(self, event: Event) -> None:
        """喂一条 CDP 事件。非 ``Network.*`` 的事件直接忽略。

        :param event: 解码后的 CDP 事件
        """
        method, p = event.method, event.params

        if method == "Network.requestWillBeSent":
            # 重定向复用同一个 requestId —— 已在集合里，不重复计入也不重置计时
            if p.get("redirectResponse") is not None:
                return
            if p.get("type") in LONG_LIVED_TYPES:
                return
            if self.frame_id is not None and p.get("frameId") not in (None, self.frame_id):
                return
            self._add(p["requestId"])

        elif method in ("Network.loadingFinished", "Network.loadingFailed"):
            self._discard(p["requestId"])

        elif method == "Network.responseReceived":
            # 迟到发现是长连接（requestWillBeSent 有时不带 type），补一次剔除
            if p.get("type") in LONG_LIVED_TYPES or _is_streaming(p.get("response") or {}):
                self._discard(p["requestId"])

    def _add(self, request_id: str) -> None:
        if request_id not in self.in_flight:
            self.in_flight.add(request_id)
            self._started[request_id] = time.monotonic()
            self._last_change = time.monotonic()

    def _discard(self, request_id: str) -> None:
        self._started.pop(request_id, None)
        if request_id in self.in_flight:
            self.in_flight.discard(request_id)
            self._last_change = time.monotonic()

    def _evict_stale(self) -> None:
        """把在飞太久的请求踢出去 —— 它们多半是流，不是没加载完。"""
        if not self.in_flight:
            return
        cutoff = time.monotonic() - self.stale_after
        for request_id in [r for r in self.in_flight if self._started.get(r, 0.0) < cutoff]:
            self._discard(request_id)

    # ------------------------------------------------------------------ #

    def reset(self, *, frame_id: str | None = None) -> None:
        """新导航开始，丢弃上一轮的全部状态。

        :param frame_id: 新的主 frame id。传 ``None`` 保持原有过滤设置不变
        """
        self.in_flight.clear()
        self._started.clear()
        if frame_id is not None:
            self.frame_id = frame_id
        self._last_change = time.monotonic()

    @property
    def quiet_for(self) -> float:
        """当前已经静默了多少秒。仍有在飞请求时恒为 0。"""
        self._evict_stale()
        if self.in_flight:
            return 0.0
        return time.monotonic() - self._last_change

    def is_idle(self, quiet: float) -> bool:
        """在飞集合为空、且已经静默够久了吗？

        :param quiet: 集合空掉之后还要再静默多少秒才算空闲
        """
        self._evict_stale()
        return not self.in_flight and self.quiet_for >= quiet


def _is_streaming(response: dict) -> bool:
    """分块且没有 Content-Length —— 长轮询 / SSE-over-fetch / gRPC-web 的形状。"""
    headers = {k.lower(): v for k, v in (response.get("headers") or {}).items()}
    return "chunked" in str(headers.get("transfer-encoding", "")).lower() and (
        "content-length" not in headers
    )
