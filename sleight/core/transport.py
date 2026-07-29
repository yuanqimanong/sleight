"""同步 WebSocket 传输。

三个原语：

``call``           发出并等待该 id 的响应。
``send_no_wait``   分配 id、发出、立即返回。**拟人轨迹必须走这个。**
``flush``          顺序屏障：排空所有未决响应，任一为 error 则抛出。

为什么需要 ``send_no_wait``：远程 CDP（SSH 隧道 / 跨区域）的 RTT 是 50–300 ms，
一次拟人点击有 20–60 个 ``mouseMoved``。每点都等响应 = 一次点击 20 次 RTT，
本地丝滑、远程慢动作。单条 WS 上 CDP 保证按序处理，所以 fire-and-forget 不会乱序；
**本地 sleep 才是拟人节奏的来源**，它同时天然掩盖了 RTT。
"""

from __future__ import annotations

import logging
import socket
import threading
import time
from collections import deque
from collections.abc import Iterator
from contextlib import suppress
from typing import Any

import websocket

from . import protocol
from ._redact import redact_url
from .errors import AuthError, ConnectionError, LeaseLost, ProtocolError, TimeoutError

log = logging.getLogger("sleight.transport")

__all__ = ["Transport"]

DEFAULT_TIMEOUT = 30.0
MAX_INFLIGHT = 512
MAX_EVENT_BUFFER = 10_000


class Transport:
    """一条到浏览器的 CDP 连接。

    **不是线程安全的，也不需要是** —— 一个实例同时只给一个 Session，每个线程自己
    ``lease()`` 自己的实例。构造线程之外调用会直接报错，而不是让 WebSocket 静默错帧
    （那种 bug 极难排查）。唯一的例外是 :meth:`close`，租约续租线程需要用它来快速
    掐断连接。

    一般不直接构造，用 :meth:`connect`。

    :param ws: 已经握手完成的 websocket-client 连接
    :param url: 原始 URL，只用于错误消息（会先脱敏）
    """

    def __init__(self, ws: websocket.WebSocket, *, url: str = "") -> None:
        self._ws = ws
        self._url = url
        self._ids = protocol.IdAllocator()
        self._inflight: set[int] = set()                       # 已发出、响应未到
        self._methods: dict[int, str] = {}                     # id -> method，用于错误定位
        self._results: dict[int, protocol.Response] = {}       # 已到、未消费
        self._abandoned: set[int] = set()                      # 超时放弃的 id，响应到了直接丢
        # 事件**按 sessionId 分桶**。共用一个队列会让先排空的那个 Session 把别人的
        # 事件一并弹走并丢掉 —— 另一个 Session 就永远等不到自己的 load。
        self._events: dict[str | None, deque[protocol.Event]] = {}
        self._owner = threading.get_ident()
        self._closed = False
        self._severed = False

    # ------------------------------------------------------------------ #
    # 连接
    # ------------------------------------------------------------------ #

    @classmethod
    def connect(
        cls,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> Transport:
        """连一个浏览器级 CDP WebSocket 端点。

        :param url: ``ws://`` 或 ``wss://`` 地址
        :param headers: 握手请求头。CloakBrowser 这类后端**必须**带
            ``Authorization``，凭据塞不进 URL
        :param timeout: 握手与后续读操作的 socket 超时，秒
        :returns: 已连上的 Transport
        :raises AuthError: 握手被 401 / 403 拒绝
        :raises ConnectionError: 连不上，或握手因别的原因失败
        """
        header_list = [f"{k}: {v}" for k, v in (headers or {}).items()]
        try:
            ws = websocket.create_connection(
                url,
                header=header_list,
                timeout=timeout,
                max_size=None,          # 截图 / 大 DOM 帧可以到几 MB
                enable_multithread=False,
                suppress_origin=True,
            )
        except websocket.WebSocketBadStatusException as exc:
            # CloakBrowser 的 WS 握手不带 Bearer 直接 403
            if exc.status_code in (401, 403):
                raise AuthError(
                    f"CDP websocket handshake rejected ({exc.status_code}); "
                    f"check the auth token: {redact_url(url)}"
                ) from exc
            raise ConnectionError(
                f"CDP websocket handshake failed ({exc.status_code}): {redact_url(url)}"
            ) from exc
        except (OSError, websocket.WebSocketException) as exc:
            raise ConnectionError(f"cannot reach CDP endpoint {redact_url(url)}: {exc}") from exc
        return cls(ws, url=url)

    @property
    def closed(self) -> bool:
        """连接是否已关闭。关闭后任何操作都会抛。"""
        return self._closed

    @property
    def severed(self) -> bool:
        """连接是不是因为**租约失效**被掐断的（区别于普通网络故障）。"""
        return self._severed

    def close(self, *, severed: bool = False) -> None:
        """幂等。可以从其它线程调用（租约失效时需要立刻掐断）。

        :param severed: 标记这是租约失效导致的强制掐断。之后所有操作抛
            :class:`LeaseLost` 而不是 ``ConnectionError`` —— 否则调用方看到的错误
            和隧道抖动一模一样，分不清"我不再持有这个浏览器"和"网络闪了一下" ——
            前者绝不能重试，后者可以
        """
        if severed:
            self._severed = True
        if self._closed:
            return
        self._closed = True
        try:
            # 先 shutdown 底层 socket。WebSocket.close() 要走 send + recv 的关闭握手，
            # 而 owner 线程此刻多半正阻塞在 recv 里；只有 shutdown 能立刻把它叫醒。
            if (sock := getattr(self._ws, "sock", None)) is not None:
                with suppress(OSError):
                    sock.shutdown(socket.SHUT_RDWR)
            self._ws.close()
        except Exception:
            log.debug("error while closing websocket", exc_info=True)

    def __enter__(self) -> Transport:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------ #
    # 三个原语
    # ------------------------------------------------------------------ #

    def call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        session_id: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> dict[str, Any]:
        """发一条命令并等它的响应。

        等待期间收到的**事件**会进缓冲队列，不会丢。

        :param method: CDP 方法名
        :param params: 参数对象
        :param session_id: 目标会话；``None`` 表示浏览器级命令
        :param timeout: 秒
        :returns: 响应的 ``result``
        :raises ProtocolError: CDP 返回了 error
        :raises TimeoutError: 超时。该 id 会被放弃，不会拖累后续的 flush
        :raises ConnectionError: 连接断了
        :raises LeaseLost: 连接是因为租约失效被掐断的
        """
        msg_id = self.send_no_wait(method, params, session_id=session_id)
        return self._await_id(msg_id, timeout=timeout).unwrap(method)

    def send_no_wait(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        session_id: str | None = None,
    ) -> int:
        """分配 id、发出、**立即返回**，不等响应。

        拟人轨迹一次点击有 20–60 个 ``mouseMoved``；每点都等响应的话，跨国链路上
        一次点击就是 20–60 次 RTT。单条 WS 上 CDP 保证按序处理，所以轨迹段
        fire-and-forget 不会乱序。响应稍后由 :meth:`flush` 或 :meth:`call` 收割。

        :param method: CDP 方法名
        :param params: 参数对象
        :param session_id: 目标会话；``None`` 表示浏览器级命令
        :returns: 这条消息的 id
        :raises ConnectionError: 发送失败
        """
        self._check_open()
        self._check_thread()

        # 未决集合不能无界增长：超了先隐式排空，否则长轨迹会把内存和 socket 缓冲撑爆
        if len(self._inflight) >= MAX_INFLIGHT:
            self.flush()

        msg_id = self._ids.next()
        frame = protocol.encode(msg_id, method, params, session_id)
        try:
            self._ws.send(frame)
        except (OSError, websocket.WebSocketException) as exc:
            self._closed = True
            self._raise_lost(f"CDP send failed on {method}: {exc}", cause=exc)
        self._inflight.add(msg_id)
        self._methods[msg_id] = method
        return msg_id

    def flush(self, *, timeout: float = DEFAULT_TIMEOUT) -> None:
        """顺序屏障：等所有未决响应回来，任一为 error 则抛出第一个。

        必须在离开一段拟人动作前调用 —— 它既是屏障，也是唯一能发现中途某条命令
        报错的地方（无等待发送时调用栈已经离开现场，只剩 id 能定位）。

        :param timeout: 等所有未决响应回来的总时限，秒
        :raises ProtocolError: 其中某条命令报错了，消息里带方法名
        :raises TimeoutError: 超时，消息里列出还在飞的方法名
        """
        deadline = time.monotonic() + timeout
        while self._inflight:
            if (remaining := deadline - time.monotonic()) <= 0:
                pending = sorted(self._methods[i] for i in self._inflight)
                raise TimeoutError(f"flush timed out after {timeout}s; still in flight: {pending}")
            self._read_one(timeout=remaining)

        # 全部到齐后统一扫描 —— 乱序到达的响应也不会被漏掉
        first_error: ProtocolError | None = None
        for msg_id in sorted(self._results):
            resp = self._results.pop(msg_id)
            method = self._methods.pop(msg_id, "?")
            if resp.error is not None and first_error is None:
                first_error = ProtocolError(
                    str(resp.error), code=resp.error.code, method=method
                )
        self._methods.clear()
        if first_error is not None:
            raise first_error

    # ------------------------------------------------------------------ #
    # 事件
    # ------------------------------------------------------------------ #

    def drain_events(self, session_id: str | None = None) -> Iterator[protocol.Event]:
        """取出并清空**该 session** 缓冲的事件。

        按 sessionId 分桶是必需的：共用一个队列时，先调 ``call()`` 的那个 Session 会
        把别人的事件一并弹走并丢掉（``Session._handle`` 只认自己的 sessionId），
        另一个 Session 就永远等不到自己的 ``load``。

        :param session_id: 要取哪一桶。``None`` 是浏览器级事件那一桶
        :returns: 迭代器，产出后即从缓冲中移除
        """
        if (bucket := self._events.get(session_id)) is None:
            return
        while bucket:
            yield bucket.popleft()
        self._events.pop(session_id, None)      # 空桶不留，避免 session 多了之后堆积

    def pump(self, *, timeout: float) -> bool:
        """尽力读一条消息进缓冲 —— 给轮询式等待推进用。

        :param timeout: 最多阻塞多久，秒
        :returns: 读到了返回 True，超时返回 False（不抛）
        """
        try:
            self._read_one(timeout=timeout)
        except TimeoutError:
            return False
        return True

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #

    def _await_id(self, msg_id: int, *, timeout: float) -> protocol.Response:
        deadline = time.monotonic() + timeout
        try:
            while True:
                if (resp := self._results.pop(msg_id, None)) is not None:
                    self._methods.pop(msg_id, None)
                    return resp
                if (remaining := deadline - time.monotonic()) <= 0:
                    method = self._methods.get(msg_id, "?")
                    raise TimeoutError(
                        f"timed out after {timeout}s waiting for {method} (id={msg_id})"
                    )
                try:
                    self._read_one(timeout=remaining)
                except TimeoutError:
                    continue          # socket 安静了一阵，回到上面的 deadline 判定
        except BaseException:
            # **任何**异常出口都要放弃这个 id。留在 _inflight 里的话，之后每一次
            # flush() 都要空等满一个 timeout 才报错 —— 而 InputDriver 每个动作末尾
            # 都 flush 一次，一次丢响应就把整个 Session 变成废的。
            self._abandon(msg_id)
            raise

    def _abandon(self, msg_id: int) -> None:
        self._inflight.discard(msg_id)
        self._methods.pop(msg_id, None)
        self._results.pop(msg_id, None)
        self._abandoned.add(msg_id)

    def _read_one(self, *, timeout: float) -> None:
        self._check_open()
        self._ws.settimeout(max(timeout, 0.001))
        try:
            raw = self._ws.recv()
        except websocket.WebSocketTimeoutException as exc:
            raise TimeoutError(f"no CDP frame within {timeout:.1f}s") from exc
        except (OSError, websocket.WebSocketException) as exc:
            self._closed = True
            self._raise_lost(f"CDP connection lost: {exc}", cause=exc)

        if not raw:
            self._closed = True
            self._raise_lost("CDP connection closed by peer")

        msg = protocol.decode(raw)
        if isinstance(msg, protocol.Response):
            self._inflight.discard(msg.id)
            if msg.id in self._abandoned:        # 迟到的响应，早已放弃，直接丢
                self._abandoned.discard(msg.id)
                return
            self._results[msg.id] = msg
        else:
            bucket = self._events.get(msg.session_id)
            if bucket is None:
                bucket = self._events.setdefault(msg.session_id, deque(maxlen=MAX_EVENT_BUFFER))
            bucket.append(msg)

    def _raise_lost(self, message: str, *, cause: BaseException | None = None) -> None:
        """租约失效导致的掉线要抛 LeaseLost，普通掉线抛 ConnectionError。"""
        exc = LeaseLost(f"lease was lost and the connection severed ({message})") if self._severed \
            else ConnectionError(message)
        raise exc from cause

    def _check_open(self) -> None:
        if self._closed:
            self._raise_lost("transport is closed")

    def _check_thread(self) -> None:
        if threading.get_ident() != self._owner:
            raise RuntimeError(
                "Transport is not thread-safe: created on thread "
                f"{self._owner}, used from {threading.get_ident()}. Lease a separate "
                "instance per thread instead of sharing a Session."
            )
