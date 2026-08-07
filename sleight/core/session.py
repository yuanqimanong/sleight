"""Session：一个 tab 上的操作会话。

**默认创建自有 target**，退出时关闭。接管既有 tab 必须显式 :meth:`Session.attach`。

不去挑 ``Target.getTargets()`` 里第一个 ``type=="page"``：target 顺序没有业务语义，
可能选中用户正在用的页面、扩展页、或上次任务的遗留页；叠加"只关自己创建的 tab"这条
规则，结果就是默认会修改一个别人的页面、退出时既不恢复也不关闭。

「使用既有持久化 Context」≠「接管既有 tab」—— ``Target.createTarget`` 不带
``browserContextId`` 就落在默认 context 里，Cookie 和登录态照样继承。
"""

from __future__ import annotations

import base64
import contextlib
import ipaddress
import json
import logging
import time
from collections.abc import Callable, Iterable, Iterator
from fnmatch import fnmatch
from random import Random
from typing import Any
from urllib.parse import urlsplit

from .element import Element
from .errors import ElementError, ProtocolError, SleightError, TimeoutError
from .human.presets import HumanProfile
from .input import HumanSwitch, InputDriver
from .netidle import NetworkIdleTracker
from .protocol import Event
from .resources import (
    RESOURCE_TYPES,
    BlockStats,
    DedupeKey,
    NetworkResource,
    ResourceTracker,
)
from .transport import Transport
from .types import Box, ClearReport, Condition, DomReady, Point, StorageType

log = logging.getLogger("sleight.session")

__all__ = [
    "BlockStats",
    "ClearReport",
    "NetworkResource",
    "Selectable",
    "Session",
    "StorageType",
]

_POLL_MIN = 0.10
_POLL_MAX = 0.25
_PUMP_SLICE = 0.05

#: :meth:`Session.exit_ip` 的默认端点。**必须是纯文本响应** —— 拿查询站的首页去
#: innerText 里挑 IP，第一个未必是出口地址
EXIT_IP_ENDPOINTS = (
    "https://api.ipify.org",
    "https://checkip.amazonaws.com",
    "https://icanhazip.com",
)

#: 无字段的 frozen dataclass，共享一个实例即可（也让 ruff B008 满意）
_DEFAULT_WAIT = DomReady()

#: :meth:`Session.clear_site_data` 的默认清理范围。**只清 cookie 是不够的** ——
#: 反检测服务的设备标识在 localStorage / indexedDB 里都有副本，会立刻把 cookie 还原
_DEFAULT_CLEAR_TYPES = (
    StorageType.COOKIES,
    StorageType.LOCAL_STORAGE,
    StorageType.INDEXEDDB,
    StorageType.CACHE_STORAGE,
    StorageType.SERVICE_WORKERS,
)


def _normalize_origin(origin: str) -> str:
    """``https://host/a/b?c`` → ``https://host``。

    CDP 的 ``clearDataForOrigin`` 收的是 origin。传个带 path 的进去它**不报错也不
    生效** —— 归一化掉，比让人自己发现这件事强。

    :param origin: 任意 URL 或 origin
    :returns: ``scheme://netloc``
    :raises ValueError: 缺 scheme 或 host
    """
    parts = urlsplit(origin)
    if not parts.scheme or not parts.netloc:
        raise ValueError(
            f"origin={origin!r} needs a scheme and a host, e.g. 'https://example.com'"
        )
    return f"{parts.scheme}://{parts.netloc}"


def _same_document(here: str, there: str) -> bool:
    """两个地址只差 fragment 吗 —— 那样的跳转不产生任何 lifecycle 事件。

    :param here: 当前地址
    :param there: 目标地址
    :returns: 是同文档导航吗
    """
    a, b = urlsplit(here), urlsplit(there)
    return a[:4] == b[:4] and a.fragment != b.fragment


#: 交互方法收的目标形态。叫 ``Selectable`` 而不是 ``Target`` —— 这个文件里
#: ``Target.createTarget`` / ``Target.attachToTarget`` 满地都是，同名会读岔
Selectable = str | Element


class Session:
    """一个 tab 上的操作会话。

    **不是线程安全的，也不需要是** —— 一个实例同时只给一个 Session，每个线程
    ``lease()`` 自己的实例。

    一般不直接构造，用 :meth:`create` / :meth:`attach`，或者
    :meth:`InstanceHandle.session() <sleight.pool.InstanceHandle.session>`。

    :param transport: 已连上的浏览器级 WebSocket
    :param session_id: ``Target.attachToTarget`` 返回的 CDP sessionId
    :param target_id: 这个会话操作的 target
    :param owned_target: True 表示 target 是自己建的，:meth:`close` 时要关掉它；
        False 表示接管的，只 detach
    :param track_network: 是否 ``Network.enable``。关掉就用不了
        :class:`~sleight.core.types.NetworkIdle` 和 :meth:`cookies`
    :param human: 本会话的默认拟人档位。``False`` 全部直通，``True`` 用 DEFAULT
        预设，也可以直接给一个 :class:`~sleight.core.human.presets.HumanProfile`
    :param rng: 随机源。传固定 seed 的 :class:`random.Random` 可复现整段交互
    """

    def __init__(
        self,
        transport: Transport,
        session_id: str,
        target_id: str,
        *,
        owned_target: bool,
        track_network: bool = True,
        human: bool | HumanProfile = False,
        rng: Random | None = None,
    ) -> None:
        self._t = transport
        self._sid = session_id
        self._target_id = target_id
        self._owned = owned_target
        self._closed = False

        # 导航纪元：绑 loaderId，杜绝上一次导航的迟到事件满足这一次的等待
        self._loader_id: str | None = None
        self._frame_id: str | None = None
        self._lifecycle: set[str] = set()
        self._netidle = NetworkIdleTracker()
        self._track_network = track_network
        # 事件观察者。有了它就不必去 monkeypatch _handle —— 那是私有方法，而且补丁
        # 之间会互相覆盖
        self._observers: list[Callable[[Event], None]] = []

        self._t.call("Page.enable", session_id=self._sid)
        self._t.call("Runtime.enable", session_id=self._sid)
        self._t.call("Page.setLifecycleEventsEnabled", {"enabled": True}, session_id=self._sid)
        if track_network:
            self._t.call("Network.enable", session_id=self._sid)
        self.drain()

        self._input = InputDriver(self, default_human=human, rng=rng)

    # ------------------------------------------------------------------ #
    # 构造
    # ------------------------------------------------------------------ #

    @classmethod
    def create(
        cls, transport: Transport, *, browser_context_id: str | None = None, **kw: Any
    ) -> Session:
        """新建一个 ``about:blank`` tab 并接管它 —— 默认路径。

        不给 ``browser_context_id`` 就落在默认 browser context 里，Cookie 和登录态
        照样继承。

        :param transport: 浏览器级 WebSocket
        :param browser_context_id: 把 tab 建在这个 browser context 里。一般不直接传 ——
            用 :meth:`InstanceHandle.context() <sleight.pool.InstanceHandle.context>`，
            它会连带管好 context 的销毁
        :param kw: 透传给构造函数（``human`` / ``rng`` / ``track_network``）
        :returns: ``owned_target=True`` 的 Session，退出时会关掉这个 tab
        """
        params: dict[str, Any] = {"url": "about:blank"}
        if browser_context_id is not None:
            params["browserContextId"] = browser_context_id
        tid = transport.call("Target.createTarget", params)["targetId"]
        try:
            sid = transport.call(
                "Target.attachToTarget", {"targetId": tid, "flatten": True}
            )["sessionId"]
            return cls(transport, sid, tid, owned_target=True, **kw)
        except BaseException:
            # 建了 target 却没接上，不能把它留在浏览器里泄漏
            try:
                transport.call("Target.closeTarget", {"targetId": tid})
            except SleightError:
                log.debug("could not clean up orphaned target %s", tid, exc_info=True)
            raise

    @classmethod
    def attach(cls, transport: Transport, target_id: str, **kw: Any) -> Session:
        """接管一个既有 tab —— 必须显式选择。

        :param transport: 浏览器级 WebSocket
        :param target_id: 要接管的 target id，可从
            :meth:`InstanceHandle.targets() <sleight.pool.InstanceHandle.targets>` 拿
        :param kw: 透传给构造函数
        :returns: ``owned_target=False`` 的 Session，退出时只 detach 不关闭
        """
        sid = transport.call(
            "Target.attachToTarget", {"targetId": target_id, "flatten": True}
        )["sessionId"]
        return cls(transport, sid, target_id, owned_target=False, **kw)

    def __enter__(self) -> Session:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def __repr__(self) -> str:
        own = "owned" if self._owned else "attached"
        return f"<Session {self._target_id[:8]} {own}{' closed' if self._closed else ''}>"

    # ------------------------------------------------------------------ #
    # 给 InputDriver 用的内部接口
    # ------------------------------------------------------------------ #

    @property
    def target_id(self) -> str:
        return self._target_id

    @property
    def owned_target(self) -> bool:
        return self._owned

    @property
    def transport(self) -> Transport:
        return self._t

    @property
    def cdp_session_id(self) -> str:
        return self._sid

    @property
    def closed(self) -> bool:
        return self._closed

    def call(self, method: str, params: dict[str, Any] | None = None, **kw: Any) -> dict[str, Any]:
        """发一条带本会话 sessionId 的 CDP 命令，等响应，并顺手排空事件。

        逃生舱 —— sleight 没封装的 CDP 命令直接从这里发。

        :param method: CDP 方法名，如 ``"Emulation.setGeolocationOverride"``
        :param params: 参数对象
        :param kw: 透传给 transport，目前只有 ``timeout``
        :returns: 响应的 ``result``
        :raises ProtocolError: CDP 返回了 error
        """
        result = self._t.call(method, params, session_id=self._sid, **kw)
        self.drain()
        return result

    def drain(self) -> None:
        """把**本会话**缓冲的事件喂给状态机。

        只取自己那一桶 —— 共用队列会让先 drain 的 Session 把别的 Session 的事件
        弹走并丢掉，对方就永远等不到自己的 ``load``。
        """
        for ev in self._t.drain_events(self._sid):
            self._handle(ev)

    def _handle(self, ev: Event) -> None:
        if ev.method == "Page.lifecycleEvent":
            # loaderId 绑定就是导航纪元 —— 上一次导航的 load 事件在这里被丢掉
            if self._loader_id is None or ev.params.get("loaderId") == self._loader_id:
                self._lifecycle.add(ev.params.get("name", ""))
        elif ev.method.startswith("Network."):
            self._netidle.feed(ev)

        for observe in self._observers:
            # 一个观察者抛异常不该把整条事件流搞停 —— 后面还有等待条件要靠它推进
            try:
                observe(ev)
            except Exception:
                log.warning("event observer %r raised; continuing", observe, exc_info=True)

    def _pump(self, timeout: float = _PUMP_SLICE) -> None:
        self._t.pump(timeout=timeout)
        self.drain()

    # ------------------------------------------------------------------ #
    # 导航与等待
    # ------------------------------------------------------------------ #

    def open(self, url: str, *, wait: Condition = _DEFAULT_WAIT, timeout: float = 60) -> None:
        """导航并等待条件满足。

        :param url: 目标地址
        :param wait: 等待条件，默认 :class:`~sleight.core.types.DomReady`。
            同文档导航（hash 路由）不产生任何 lifecycle 事件，此时
            ``DomReady`` / ``Load`` 直接返回，其余条件照常轮询
        :param timeout: 秒，覆盖导航命令和等待两段
        :raises TimeoutError: 条件没在 ``timeout`` 内满足
        :raises SleightError: 浏览器直接拒绝了这次导航（DNS 失败、协议错误等）
        """
        # 不走 self.call：必须先拿到新的 loaderId 再排空事件，否则新导航的 lifecycle
        # 事件会因为 loaderId 还是旧的而被当成迟到事件丢掉
        self._renavigate(
            lambda: self._t.call(
                "Page.navigate", {"url": url}, session_id=self._sid, timeout=timeout
            ),
            what=url, same_document=None, wait=wait, timeout=timeout,
        )

    def reload(
        self,
        *,
        ignore_cache: bool = False,
        wait: Condition = _DEFAULT_WAIT,
        timeout: float = 60,
    ) -> None:
        """重新加载当前页面。

        **和 ``open(当前地址)`` 不是一回事。** ``Page.navigate`` 到同一个 URL 会命中
        缓存，语义上是"再去一次"；``reload`` 是"刷新"。想复现"手动按 F5"的行为
        （比如观察每次刷新出口 IP 变不变）就得用这个，别再靠"URL 挂个唯一查询参数"
        去近似 —— 那改的是 URL，不是缓存语义。

        ⚠️ **目标地址会重定向时，别用 ``DomReady`` / ``Load`` 当完成信号。** 一次
        重定向是两次文档提交，中间那个文档也可能发出自己的 ``DOMContentLoaded``，于是
        这个方法在链条走完之前就返回了。真机上量到过 3/8 的复现率（``http://`` 跳
        ``https://``），换成直接访问 ``https://`` 是 0/8。要可靠就等页面自己的东西：

            >>> s.reload(wait=Selector("#article-body"), timeout=60)

        :param ignore_cache: True 相当于 Ctrl+Shift+R，绕过缓存重新拉所有资源
        :param wait: 等待条件，默认 :class:`~sleight.core.types.DomReady`
        :param timeout: 秒，覆盖导航和等待两段
        :raises TimeoutError: 条件没在 ``timeout`` 内满足
        """
        self._renavigate(
            lambda: self._t.call(
                "Page.reload", {"ignoreCache": ignore_cache},
                session_id=self._sid, timeout=timeout,
            ),
            what="reload", same_document=False, wait=wait, timeout=timeout,
        )

    def back(self, *, steps: int = 1, wait: Condition = _DEFAULT_WAIT,
             timeout: float = 60) -> None:
        """后退。

        :param steps: 退几步。必须 ≥ 1
        :param wait: 等待条件
        :param timeout: 秒
        :raises ValueError: ``steps < 1``
        :raises SleightError: 历史里没有那么多可退的条目
        """
        self._history_go(-steps, wait=wait, timeout=timeout)

    def forward(self, *, steps: int = 1, wait: Condition = _DEFAULT_WAIT,
                timeout: float = 60) -> None:
        """前进。参数同 :meth:`back`。"""
        self._history_go(steps, wait=wait, timeout=timeout)

    def history(self) -> tuple[int, list[dict[str, Any]]]:
        """浏览历史。

        :returns: ``(当前下标, 条目列表)``，条目是 CDP 的 ``NavigationEntry``
        """
        r = self.call("Page.getNavigationHistory")
        return int(r.get("currentIndex", 0)), list(r.get("entries") or [])

    def _history_go(self, delta: int, *, wait: Condition, timeout: float) -> None:
        if abs(delta) < 1:
            raise ValueError(f"steps must be at least 1, got {abs(delta)}")
        index, entries = self.history()
        target = index + delta
        if not 0 <= target < len(entries):
            direction = "back" if delta < 0 else "forward"
            raise SleightError(
                f"cannot go {direction} {abs(delta)} step(s): at entry {index} of "
                f"{len(entries)}. Check session.history() first."
            )

        here = entries[index].get("url", "")
        there = entries[target].get("url", "")
        entry_id = entries[target]["id"]
        self._renavigate(
            lambda: self._t.call(
                "Page.navigateToHistoryEntry", {"entryId": entry_id},
                session_id=self._sid, timeout=timeout,
            ),
            what=there or "history entry",
            # 只差 fragment 的两条历史是**同文档**导航，不会产生任何 lifecycle 事件。
            # 判定放在这里而不是靠"等不到就当成同文档"：后者会把一次慢提交读成成功
            same_document=_same_document(here, there),
            wait=wait, timeout=timeout,
        )

    def _renavigate(
        self,
        send: Callable[[], dict[str, Any]],
        *,
        what: str,
        same_document: bool | None,
        wait: Condition,
        timeout: float,
    ) -> None:
        """所有导航的公共骨架：换导航纪元，然后等条件。

        「导航纪元」= 把 ``_loader_id`` 绑到当前文档，好让上一次导航的迟到 lifecycle
        事件立刻满足这一次的等待这种事不发生。建立它有两个来源，都得要：

        * ``Page.navigate`` 的响应里直接给 loaderId；
          ``Page.reload`` / ``Page.navigateToHistoryEntry`` **不给**；
        * ``Page.frameNavigated`` 事件。**每一次提交都要重新绑，不能只认第一条** ——
          一次重定向就会产生两条（``http://example.com/`` → ``https://…`` 实测如此），
          真正的生命周期事件挂在**后一个** loaderId 上。只认第一条的话一切看着都对，
          就是 ``DomReady`` 永远等不到。这一条对 ``open()`` 同样成立：响应里那个
          loaderId 未必是最终文档的，走 Fetch 拦截时尤其容易差这一跳。

        换纪元必须**在观察者里当场**做：同一轮 drain 里紧跟着的 ``DOMContentLoaded``
        要拿新 loaderId 去比对，晚一步就被丢掉。观察者一直挂到 :meth:`wait` 结束。

        :param send: 发出导航命令，返回 CDP 的 result
        :param what: 出错信息里用来指代这次导航的东西（URL / ``"reload"``）
        :param same_document: ``True`` 明确是同文档跳转，``False`` 明确不是，
            ``None`` 表示看响应里有没有 loaderId（``Page.navigate`` 用这个）
        :param wait: 等待条件
        :param timeout: 秒
        """
        committed: list[str] = []

        def rebind(loader: str, frame_id: str | None) -> None:
            committed.append(loader)
            self._loader_id = loader
            if frame_id is not None:
                self._frame_id = frame_id
            self._lifecycle.clear()
            self._netidle.reset(frame_id=self._frame_id)

        def commit(ev: Event) -> None:
            if ev.method != "Page.frameNavigated":
                return
            frame = ev.params.get("frame") or {}
            if self._frame_id is not None and frame.get("id") != self._frame_id:
                return                                  # 子 frame 的导航不算
            rebind(frame.get("loaderId") or "", frame.get("id"))

        self.drain()                                    # 发命令之前先清一遍
        with self.observe_events(commit):
            result = send()
            if err := result.get("errorText"):
                raise SleightError(f"navigation to {what} failed: {err}")

            loader = result.get("loaderId")
            if same_document is None:
                # **同文档导航**（fragment / hash 路由）不返回 loaderId，Chrome 也不会
                # 为它发任何 lifecycle 事件 —— 等 DomReady/Load 就是等到超时。
                #
                # 也**不能**把 _loader_id 置成 None：那在 _handle 里的含义是"接受一切
                # loaderId"，等于把纪元过滤器永久解除武装。
                same_document = loader is None

            # **先排空上一纪元的残留，再换纪元。** 响应回来之前缓冲区里可能还压着旧
            # 文档的 Network.requestWillBeSent —— 顺序反了的话 reset() 刚清空集合，
            # 紧接着的 drain() 又把它们塞回去，NetworkIdle 会等一批永远不结束的旧请求。
            self.drain()

            # 观察者已经从事件里认到更新的提交时，别用响应里那个旧的覆盖回去
            if loader is not None and not committed:
                rebind(loader, result.get("frameId"))

            self.drain()                                # 新纪元的早到事件

            if same_document:
                if wait.kind in ("domready", "load"):
                    return
            elif not committed:
                deadline = time.monotonic() + timeout
                while not committed and (left := deadline - time.monotonic()) > 0:
                    self._pump(timeout=min(_PUMP_SLICE, left))

            # wait 也在观察者作用域内 —— 重定向链的后续提交要能继续换纪元
            self.wait(wait, timeout=timeout)

    def wait(self, cond: Condition, *, timeout: float = 30) -> None:
        """等一个条件成立。一次只收一个条件，要复合就连着调两次。

        轮询是脚本行为不是用户行为，不需要拟人化。

        :param cond: :class:`~sleight.core.types.DomReady` /
            :class:`~sleight.core.types.Load` / :class:`~sleight.core.types.Text` /
            :class:`~sleight.core.types.Selector` / :class:`~sleight.core.types.Gone` /
            :class:`~sleight.core.types.NetworkIdle`
        :param timeout: 秒
        :raises TimeoutError: 超时。异常的 ``last_value`` 带着最后一次求值结果
        :raises ValueError: 条件类型不认识
        """
        deadline = time.monotonic() + timeout
        poll = _POLL_MIN
        last: Any = None

        while True:
            self.drain()
            done, last = self._check(cond)
            if done:
                return
            if (remaining := deadline - time.monotonic()) <= 0:
                raise TimeoutError(
                    f"timed out after {timeout}s waiting for {cond}", last_value=last
                )
            # 事件类条件靠 pump 推进，轮询类条件靠 sleep + 重新求值
            self._pump(timeout=min(poll, remaining))
            poll = min(poll * 1.3, _POLL_MAX)

    def _check(self, cond: Condition) -> tuple[bool, Any]:
        kind = cond.kind
        if kind == "domready":
            return ("DOMContentLoaded" in self._lifecycle, sorted(self._lifecycle))
        if kind == "load":
            return ("load" in self._lifecycle, sorted(self._lifecycle))
        if kind == "text":
            body = self.eval("document.body ? document.body.innerText : ''") or ""
            return (cond.value in body, f"{len(body)} chars of innerText")  # type: ignore[attr-defined]
        if kind == "selector":
            n = self._count(cond.value)  # type: ignore[attr-defined]
            return (n > 0, n)
        if kind == "gone":
            n = self._count(cond.value)  # type: ignore[attr-defined]
            return (n == 0, n)
        if kind == "netidle":
            return (
                self._netidle.is_idle(cond.quiet),  # type: ignore[attr-defined]
                sorted(self._netidle.in_flight)[:5],
            )
        raise ValueError(f"unknown wait condition: {cond!r}")

    def _count(self, selector: str) -> int:
        return int(self.eval(f"document.querySelectorAll({json.dumps(selector)}).length") or 0)

    # ------------------------------------------------------------------ #
    # 事件与资源监听
    # ------------------------------------------------------------------ #

    @contextlib.contextmanager
    def observe_events(
        self, callback: Callable[[Event], None]
    ) -> Iterator[Callable[[Event], None]]:
        """临时挂一个原始 CDP 事件观察者。

        低层逃生舱 —— sleight 没建模的事件从这里拿。多数场景更该用
        :meth:`capture_resources`。

        观察者在 Session 自己的状态机（导航纪元、网络空闲）**之后**被调用，只收本会话
        的事件。它抛异常只会记一条 warning，不会打断事件流。

            >>> with session.observe_events(lambda ev: print(ev.method)):
            ...     session.open("https://example.com")

        :param callback: 收 :class:`~sleight.core.protocol.Event` 的可调用对象
        :returns: 上下文管理器，产出 ``callback`` 本身；退出时自动摘掉
        """
        self._observers.append(callback)
        try:
            yield callback
        finally:
            with contextlib.suppress(ValueError):
                self._observers.remove(callback)

    @contextlib.contextmanager
    def capture_resources(
        self,
        *,
        types: Iterable[str] | None = None,
        predicate: Callable[[NetworkResource], bool] | None = None,
        dedupe_by: DedupeKey | None = "url",
        on_discovered: Callable[[NetworkResource], None] | None = None,
    ) -> Iterator[ResourceTracker]:
        """收集页面加载过程中的网络资源。

        **库只负责给出结构化数据，筛选和输出格式是调用方的事** —— 所以这里没有任何
        打印，要看什么自己在 ``on_discovered`` 里写。

            >>> def show(r):
            ...     print(r.resource_type, r.url)
            >>> with session.capture_resources(
            ...     types={"Script", "Stylesheet"}, on_discovered=show
            ... ) as capture:
            ...     session.open(url, wait=Load())
            ...     session.pump_events(10)          # 等异步加载的那批
            >>> capture.urls("Script")
            [...]

        资源的分类可能到**响应回来**才确定（``requestWillBeSent`` 上的 ``type``
        经常缺失），所以 ``on_discovered`` 的时机是"首次满足筛选条件"，不是"首次
        看见"。

        看不到 attach 之前就已经发起的请求 —— 和
        :class:`~sleight.core.types.NetworkIdle` 是同一个语义边界。

        :param types: 只要这些 CDP ``ResourceType``（``Script`` / ``Stylesheet`` /
            ``XHR`` / ``Fetch`` / ``Image`` / ``Font`` / ``Document`` …）。``None`` = 全要
        :param predicate: 额外的自定义谓词，与 ``types`` 是**与**关系。
            例如 ``lambda r: r.status == 200``
        :param dedupe_by: ``"url"`` 每个地址只报一次（默认）；``"request_id"`` 每条
            请求一次；``None`` 不去重
        :param on_discovered: 首次匹配时的回调
        :returns: 上下文管理器，产出 :class:`~sleight.core.resources.ResourceTracker`。
            退出后 tracker 仍然可以 :meth:`~sleight.core.resources.ResourceTracker.snapshot`
        :raises ValueError: ``types`` 里有不认识的 ResourceType（拼错大小写的后果是
            静默抓不到东西，所以宁可直接报错）
        :raises SleightError: 本会话构造时 ``track_network=False``，没开 Network domain
        """
        if not self._track_network:
            raise SleightError(
                "capture_resources() needs the Network domain; this session was created "
                "with track_network=False"
            )
        tracker = ResourceTracker(
            types=frozenset(types) if types is not None else None,
            predicate=predicate,
            dedupe_by=dedupe_by,
            on_discovered=on_discovered,
        )
        with self.observe_events(tracker.feed):
            yield tracker

    def pump_events(self, duration: float, *, tick: float = 0.25) -> None:
        """原地收事件收 ``duration`` 秒，不判断任何条件。

        给"页面 load 完了，但还有一批 JS 在异步拉资源"这种场景用 —— 想等的是一段
        时间，不是一个条件。有明确条件时用 :meth:`wait`，别在这里睡固定时长。

        :param duration: 收多久，秒。``<= 0`` 直接返回
        :param tick: 单次 ``recv`` 的阻塞上限，秒。调小了响应更及时，也更占 CPU
        """
        if duration <= 0:
            return
        deadline = time.monotonic() + duration
        while (remaining := deadline - time.monotonic()) > 0:
            self._pump(timeout=min(tick, remaining))

    # ------------------------------------------------------------------ #
    # 读取
    # ------------------------------------------------------------------ #

    def eval(self, expr: str) -> Any:
        """``Runtime.evaluate``，返回 by-value 的结果。

        用 evaluate **读** DOM 是安全的（读不伪造事件）；**写交互不行** —— 那会产生
        ``isTrusted=false`` 且坐标 (0,0) 的假事件。点击一律走 :meth:`click`。

        :param expr: JS 表达式。拼用户输入进去时务必用 ``json.dumps`` 转义
        :returns: by-value 的求值结果；不可序列化的对象得到 ``None``
        :raises ProtocolError: JS 抛异常了
        """
        r = self.call(
            "Runtime.evaluate",
            {"expression": expr, "returnByValue": True, "awaitPromise": True},
        )
        if details := r.get("exceptionDetails"):
            desc = (details.get("exception") or {}).get("description") or details.get("text")
            raise ProtocolError(f"JS exception: {desc}")
        return (r.get("result") or {}).get("value")

    def content(self) -> str:
        """渲染后的 ``document.documentElement.outerHTML``。"""
        return self.eval("document.documentElement.outerHTML") or ""

    def text(self) -> str:
        """``document.body.innerText``。

        **与 HTML 文本不等价** —— 隐藏元素不计入，换行按渲染结果算。
        """
        return self.eval("document.body ? document.body.innerText : ''") or ""

    def title(self) -> str:
        """``document.title``。"""
        return self.eval("document.title") or ""

    def url(self) -> str:
        """``location.href`` —— 重定向之后的**当前**地址。"""
        return self.eval("location.href") or ""

    def viewport(self) -> tuple[int, int]:
        """实际可视区域，CSS 像素。

        以它为准而不是 framebuffer —— CloakBrowser 的 viewport 高度是
        ``screen_height − 133``，1080 的屏拿到的是 947。

        :returns: ``(innerWidth, innerHeight)``；页面还没法求值时回落到 ``(1280, 720)``
        """
        size = self.eval("[innerWidth, innerHeight]") or [1280, 720]
        return int(size[0]), int(size[1])

    # ------------------------------------------------------------------ #
    # 定位
    # ------------------------------------------------------------------ #

    def query(self, selector: str) -> Element | None:
        """第一个匹配的元素。

        **只支持主 frame 的普通 DOM**，不穿透 iframe / OOPIF / Shadow DOM。

        :param selector: CSS 选择器
        :returns: 元素；没命中返回 ``None``
        """
        el = Element(self, selector, 0)
        return el if el.exists() else None

    def query_all(self, selector: str) -> list[Element]:
        """所有匹配的元素。

        :param selector: CSS 选择器
        :returns: 按文档顺序排列；没命中返回空列表
        """
        return [Element(self, selector, i) for i in range(self._count(selector))]

    def require(self, target: Selectable) -> Element:
        """把选择器或 Element 统一成一个**确认存在**的 Element。

        :param target: CSS 选择器字符串，或已有的 Element
        :raises ElementError: 选择器没命中
        """
        el = Element(self, target, 0) if isinstance(target, str) else target
        if not el.exists():
            raise ElementError(f"no element matches {el.selector!r}[{el.index}]")
        return el

    # ------------------------------------------------------------------ #
    # 交互
    #
    # 全部委托给 InputDriver —— 它是唯一允许发输入事件的地方，也是维护鼠标位置
    # 连续性的地方。
    #
    # 每个方法的 human= 都是三态：None 继承 Session 默认，False 直通，
    # True 用 DEFAULT 预设，给 HumanProfile 就用它。
    # ------------------------------------------------------------------ #

    @property
    def cursor(self) -> Point:
        """当前鼠标位置。下一次移动从这里出发。

        首次访问时落在视口内的一个随机点，而不是 (0, 0)。
        """
        return self._input.cursor

    def click(
        self,
        target: Selectable | Point | Box,
        *,
        human: HumanSwitch = None,
        button: str = "left",
        click_count: int = 1,
    ) -> None:
        """移动过去并点击。元素不在视口里会先滚进来。

        按下之前会做**两次**命中校验（取到几何时一次、轨迹跑完时一次）—— 轨迹要跑
        300 ms 到 2 s，这期间冒出来的 cookie 横幅足以盖住目标。

        :param target: CSS 选择器 / :class:`~sleight.core.element.Element` /
            :class:`~sleight.core.types.Point` / :class:`~sleight.core.types.Box`。
            给裸坐标就不做命中校验
        :param human: 三态开关，``None`` 继承 Session 默认
        :param button: ``left`` / ``right`` / ``middle`` / ``back`` / ``forward``
        :param click_count: 连击次数
        :raises ElementError: 没命中、零尺寸、或落点被别的元素盖住
        """
        self._input.click(target, human=human, button=button, click_count=click_count)

    def double_click(self, target: Selectable | Point | Box, **kw: Any) -> None:
        """双击 —— **两次完整的按下抬起**，``detail`` 依次为 1、2。

        :param target: 同 :meth:`click`
        :param kw: 透传给 :meth:`click`（``human`` / ``button``）
        """
        self._input.click(target, click_count=2, **kw)

    def drag(
        self,
        target: Selectable | Point | Box,
        *,
        to: Selectable | Point | Box | None = None,
        by: tuple[int, int] | None = None,
        human: HumanSwitch = None,
        button: str = "left",
    ) -> Point:
        """按住 ``target`` 拖到别处。**纯鼠标事件** —— 滑块、画布、地图平移用这个。

            >>> session.drag("#slider-handle", by=(212, 0))   # 滑块验证码
            >>> session.drag(".card", to="#done-column")

        HTML5 的 ``draggable=true`` 收不到纯鼠标事件，那种用 :meth:`drag_and_drop`。

        拖拽段的 ``buttons`` 位掩码一路非零，过冲阈值也单列（滑块只有一两百像素宽，
        指针移动那档 500 px 的阈值等于永不过冲），松手前还有一段迟滞 —— **到位即松手**
        是滑块风控最爱抓的特征。

        :param target: 抓哪。选择器 / :class:`~sleight.core.element.Element` /
            :class:`~sleight.core.types.Point` / :class:`~sleight.core.types.Box`
        :param to: 拖到哪，形态同上。与 ``by`` 二选一
        :param by: ``(dx, dy)``，从实际抓取点算的相对位移
        :param human: 三态开关，``None`` 继承 Session 默认
        :param button: 按住哪个键拖
        :returns: 松手的坐标
        :raises ValueError: ``to`` / ``by`` 一个没给，或两个都给了
        :raises ElementError: 起点没命中、被遮挡，或终点元素不在视口里
        """
        return self._input.drag(target, to=to, by=by, human=human, button=button)

    def drag_and_drop(
        self,
        source: Selectable | Point | Box,
        target: Selectable | Point | Box,
        *,
        human: HumanSwitch = None,
        button: str = "left",
        native: bool | None = None,
    ) -> Point:
        """把 ``source`` 拖到 ``target`` 上，HTML5 原生拖放与 JS 实现都吃。

        默认自适应：按下、起步，然后看浏览器认不认这是原生拖放，再决定剩下的轨迹发
        ``dragOver``/``drop`` 还是继续发 ``mouseMoved``。两种实现对**对方**的事件毫无
        反应，猜错的表现是"一切正常但什么都没发生" —— 所以这里不猜。

        :param source: 拖谁
        :param target: 拖到哪
        :param human: 三态开关，``None`` 继承 Session 默认
        :param button: 按住哪个键拖
        :param native: ``None`` 自适应（默认）；``True`` 要求必须原生可拖，否则报错；
            ``False`` 只发鼠标事件，等价于 :meth:`drag`
        :returns: 松手的坐标
        :raises ElementError: 起点没命中、终点不在视口里，或 ``native=True`` 但元素
            不是原生可拖的
        """
        return self._input.drag_and_drop(
            source, target, human=human, button=button, native=native
        )

    def type(
        self,
        target: Selectable | None,
        text: str,
        *,
        human: HumanSwitch = None,
        clear: bool = False,
    ) -> None:
        """点击聚焦后逐字符输入。

        逐字符 keyDown/keyUp，间隔按 digraph 类型取分布。非 ASCII 成段走
        ``Input.insertText``（注意：它不产生 composition 事件，不等价于真 IME）。

        :param target: 先点它聚焦。``None`` 表示直接打到当前焦点上
        :param text: 要输入的文本
        :param human: 三态开关
        :param clear: 先 Ctrl+A 再 Backspace 清空。执行前会强制确认焦点真的落在
            ``target`` 上 —— 点一个 ``<div>`` 焦点会留在原处，那样清空的是别的输入框
        :raises ElementError: 目标不存在、被遮挡，或 ``clear=True`` 时焦点没落对
        """
        self._input.type(target, text, human=human, clear=clear)

    def press(self, key: str, *, human: HumanSwitch = None) -> None:
        """按一个键或组合键，打到当前焦点上。

        修饰键自己的 keyDown / keyUp 会真的发出去，不是只设 modifiers 位。

        :param key: ``"Enter"`` / ``"Ctrl+A"`` / ``"ctrl+shift+k"``，``+`` 分隔，
            大小写不敏感
        :param human: 三态开关，只影响按住时长
        :raises ValueError: 键名不认识
        """
        self._input.press(key, human=human)

    def scroll(self, dy: int, *, human: HumanSwitch = None) -> None:
        """在光标当前位置滚轮。滚的是**光标下面**那个容器。

        :param dy: 距离，px。正数向下
        :param human: 三态开关。拟人模式切成多个 80–160 px 的小步
        """
        self._input.scroll(dy, human=human)

    def scroll_into_view(self, target: Selectable, *, human: HumanSwitch = None) -> None:
        """把元素滚进视口。

        拟人模式发真实 ``mouseWheel`` 分步滚动；直通模式走
        ``DOM.scrollIntoViewIfNeeded``（瞬时，无滚轮事件）。只滚垂直方向。

        :param target: 选择器或 Element
        :param human: 三态开关
        :raises ElementError: 滚不动 —— 多半在嵌套滚动容器里，或横向出界
        """
        self._input.scroll_into_view(target, human=human)

    def hover(self, target: Selectable | Point | Box, *, human: HumanSwitch = None) -> None:
        """只移动光标过去，不按下。

        :param target: 同 :meth:`click`
        :param human: 三态开关
        """
        self._input.hover(target, human=human)

    def select_option(
        self, target: Selectable, *, value: str | None = None, label: str | None = None
    ) -> str:
        """选中 ``<select>`` 里的一项。

            >>> s.select_option("#country", value="HK")
            >>> s.select_option("#country", label="中国香港")

        **不能只改 ``el.value``。** 那样页面上的 ``change`` handler 一个都不会跑 ——
        联动的二级下拉不刷新、表单校验不触发，而页面看上去是对的。这里改完会补发
        ``input`` 和 ``change``（都 ``bubbles``），和用户真选一样。

        用 evaluate 而不是键鼠：原生下拉框展开的是**操作系统的**弹出列表，它根本不在
        页面里，鼠标事件够不着。这是少数几个必须用 JS 的地方。

        :param target: ``<select>`` 元素。选择器或 :class:`~sleight.core.element.Element`
        :param value: 按 ``<option value>`` 选。与 ``label`` 二选一
        :param label: 按选项显示文本选（去空白后精确匹配）
        :returns: 选中项的 ``value``
        :raises ValueError: ``value`` / ``label`` 一个没给或给了两个
        :raises ElementError: 元素不存在、不是 ``<select>``、或没有匹配的选项
        """
        if (value is None) == (label is None):
            raise ValueError("select_option() takes exactly one of value= / label=")

        element = self.require(target)
        wanted = json.dumps(value if value is not None else label)
        by = "value" if value is not None else "label"
        result = element._eval(f"""
            if (el.tagName !== 'SELECT') return {{error: 'not a <select>, it is a <' +
                el.tagName.toLowerCase() + '>'}};
            const want = {wanted}, by = {json.dumps(by)};
            const hit = Array.from(el.options).find(
                o => (by === 'value' ? o.value : o.textContent.trim()) === want);
            if (!hit) return {{error: 'no option with ' + by + '=' + JSON.stringify(want) +
                '; available: ' + JSON.stringify(Array.from(el.options).map(
                    o => by === 'value' ? o.value : o.textContent.trim()))}};
            hit.selected = true;
            // 两个都要发，而且都要 bubbles —— 只发 change 的话用 input 监听的框架收不到
            el.dispatchEvent(new Event('input', {{bubbles: true}}));
            el.dispatchEvent(new Event('change', {{bubbles: true}}));
            return {{value: hit.value}};
        """)
        if result is None:
            raise ElementError(f"{element!r} is gone")
        if error := result.get("error"):
            raise ElementError(f"{element!r}: {error}")
        return str(result["value"])

    def upload_file(
        self, target: Selectable, *paths: str, allow_empty: bool = False
    ) -> None:
        """给 ``<input type=file>`` 塞文件。

            >>> s.upload_file("#avatar", "/data/uploads/me.png")

        ⚠️ **路径是「浏览器所在那台机器」上的路径**，不是跑脚本这台。浏览器在容器里
        跑的时候，文件得先进容器（挂卷或 ``docker cp``）。

        ⚠️ **浏览器不校验路径存在。** 实测传一个不存在的路径，CDP 不报错，
        ``FileList`` 里照样多出一项，只是 ``size`` 为 0 —— 表单于是上传了一个空文件，
        而脚本这边一切正常。所以这里会读回来查一遍，0 字节直接报错。真要传空文件用
        ``allow_empty=True``。

        走 ``DOM.setFileInputFiles``，不是伪造 ``change`` 事件：``FileList`` 在 JS 里
        造不出来，伪造的事件里 ``files`` 是空的。

        :param target: ``<input type=file>``
        :param paths: 一个或多个**浏览器端**的绝对路径。多个需要 ``multiple``
        :param allow_empty: 允许 0 字节的文件
        :raises ValueError: 一个路径都没给
        :raises ElementError: 元素不存在，或塞进去的文件是 0 字节（多半是路径写错了）
        :raises ProtocolError: 元素不是文件输入框
        """
        if not paths:
            raise ValueError("upload_file() needs at least one path")
        element = self.require(target)
        object_id = element.object_id()
        try:
            self.call("DOM.setFileInputFiles", {"files": list(paths), "objectId": object_id})
        finally:
            # 不释放会把节点钉在内存里
            with contextlib.suppress(SleightError):
                self.call("Runtime.releaseObject", {"objectId": object_id})

        if allow_empty:
            return
        landed = element._eval(
            "return Array.from(el.files || []).map(f => [f.name, f.size]);"
        ) or []
        if empty := [name for name, size in landed if not size]:
            raise ElementError(
                f"{element!r}: {empty} came out 0 bytes. Chrome does not check that an "
                f"upload path exists — it just makes an empty File. Note the paths are on "
                f"the machine running the browser, not this one. Pass allow_empty=True if "
                f"an empty file is really what you want. Asked for: {list(paths)}"
            )

    def set_viewport(self, width: int, height: int, *, scale: float = 1.0) -> None:
        """改视口尺寸，立即生效，不用重启实例。

            >>> s.set_viewport(1280, 2400)      # 拉高，一次性触发懒加载
            >>> s.clear_viewport()

        ⚠️ **这是渲染层的覆盖，改不了 ``screen.width`` / ``screen.height``** ——
        那两个是 profile 的指纹字段，由实例启动参数决定。所以窗口比屏幕还大这种组合
        是做得出来的，而它本身就是一个特征。临时用可以，别把它当成"改分辨率"。

        :param width: CSS 像素，必须 > 0
        :param height: CSS 像素，必须 > 0
        :param scale: 页面缩放
        :raises ValueError: 宽或高 ≤ 0
        """
        if width <= 0 or height <= 0:
            raise ValueError(f"viewport must be positive, got {width}x{height}")
        self.call("Emulation.setDeviceMetricsOverride", {
            "width": int(width), "height": int(height),
            "deviceScaleFactor": scale, "mobile": False,
        })

    def clear_viewport(self) -> None:
        """撤掉 :meth:`set_viewport` 的覆盖。幂等。

        ⚠️ **撤掉覆盖 ≠ 尺寸变回去。** 这条命令只保证"不再有覆盖"，之后窗口是多大由
        浏览器说了算。Chromium 146 + Xvnc 上实测（每次都用新 tab，重复 4 轮）：

        ==== ============== ================ ==========================
        轮次  set(900,2400)  clear 之后
        ==== ============== ================ ==========================
        #0   (900, 2400)    (1928, 957)      弹回来了，但不等于覆盖前的 (1920, 947)
        #1   (900, 2400)    **(900, 2400)**  根本没变
        #2   (900, 2400)    (1928, 957)
        #3   (900, 2400)    **(900, 2400)**
        ==== ============== ================ ==========================

        重复调 ``clear`` 没用（连发三次，结果一样）。**别依赖撤销之后的尺寸** ——
        要什么尺寸就 :meth:`set_viewport` 到什么尺寸。
        """
        self.call("Emulation.clearDeviceMetricsOverride")

    # ------------------------------------------------------------------ #
    # 杂项
    # ------------------------------------------------------------------ #

    def screenshot(
        self, path: str | None = None, *, target: Selectable | None = None
    ) -> bytes:
        """截图，PNG。

            >>> s.screenshot("page.png")                  # 整个 viewport
            >>> s.screenshot("captcha.png", target="#captcha img")

        整页时画面以**实际 viewport** 为准，不是 framebuffer。

        :param path: 给了就同时把字节写到这个文件
        :param target: 只截这个元素。不在视口里会先滚进来，然后**重新取 box**
        :returns: PNG 字节
        :raises ElementError: ``target`` 不存在或宽高为 0
        """
        params: dict[str, Any] = {"format": "png"}
        if target is not None:
            params["clip"] = self._element_clip(target)
            # clip 用的是**页面**坐标，可能落在当前 viewport 之外
            params["captureBeyondViewport"] = True

        data = base64.b64decode(self.call("Page.captureScreenshot", params)["data"])
        if path:
            with open(path, "wb") as fh:
                fh.write(data)
        return data

    def _element_clip(self, target: Selectable) -> dict[str, float]:
        """元素的截图裁剪框。

        ``Element.box()`` 是 ``getBoundingClientRect()``，**viewport** 坐标；而
        ``captureScreenshot`` 的 clip 是**页面**坐标。差一个滚动偏移 —— 忘了加就是
        滚过之后截出来的图整体偏移，而且页面没滚动时完全正常，测不出来。
        """
        element = self.require(target)
        if not element.in_viewport():
            self._input.scroll_into_view(element)
        box = element.require_box()
        scroll_x, scroll_y = self.eval("[window.scrollX, window.scrollY]") or [0, 0]
        return {
            "x": box.x + scroll_x, "y": box.y + scroll_y,
            "width": box.w, "height": box.h, "scale": 1,
        }

    def exit_ip(self, *, endpoints: Iterable[str] | None = None, timeout: float = 15.0) -> str:
        """本会话走出去的公网 IP。

            >>> with inst.context() as ctx, ctx.session() as s:
            ...     print(s.exit_ip())      # 这一轮走的哪个出口

        被拦时的第一线索。自己实现有两个坑，这里都躲开了：

        * **只用纯文本端点。** 拿 IP 查询站的首页去 ``innerText`` 里挑，第一个 IP
          未必是出口地址 —— 一组实验会因此被读成无效结论；
        * **用 :mod:`ipaddress` 校验，不手写正则。** 页面上的 ``12:34:56`` 能匹配
          大多数 IPv6 正则，``2026.08.06`` 能匹配 IPv4 正则。

        请求从**页面里**发出去，所以走的是浏览器的 socket pool 和代理 —— 这正是要
        测的东西。代价是页面的 CSP 可能挡掉它；在 ``about:blank`` 上调最稳。

        :param endpoints: 纯文本 IP 端点，按顺序试。``None`` 用内置的三个
        :param timeout: 单个端点的上限，秒
        :returns: IP 字符串
        :raises SleightError: 所有端点都没给出合法 IP，消息里列出每个端点的结果
        """
        tried: list[str] = []
        for url in endpoints if endpoints is not None else EXIT_IP_ENDPOINTS:
            try:
                raw = self.eval(
                    f"fetch({json.dumps(url)}, {{cache: 'no-store',"
                    f" signal: AbortSignal.timeout({int(timeout * 1000)})}})"
                    ".then(r => r.ok ? r.text() : null).then(t => t && t.trim().slice(0, 64))"
                )
            except (ProtocolError, TimeoutError) as exc:
                tried.append(f"{url}: {type(exc).__name__}")
                continue
            try:
                return str(ipaddress.ip_address(str(raw).strip()))
            except ValueError:
                tried.append(f"{url}: {raw!r} is not an IP address")

        raise SleightError("could not determine the exit IP. " + "; ".join(tried))

    @contextlib.contextmanager
    def block(
        self,
        *,
        types: Iterable[str] | None = None,
        url_patterns: Iterable[str] | None = None,
    ) -> Iterator[BlockStats]:
        """屏蔽请求。正文采集不需要图片、广告、字体、媒体。

            >>> with s.block(types=["Image", "Media", "Font"]) as blocked:
            ...     s.open(url)
            >>> blocked.by_type
            {'Image': 34, 'Font': 6}

        走计费住宅代理的话这直接是省钱，而且页面加载更快、超时更少。

        裁决走 :meth:`Transport.urgent_events() <sleight.core.transport.Transport.urgent_events>`
        —— 读到就回，**不进事件缓冲**。这一点是必须的而不是优化：普通事件在 ``call()``
        期间只进缓冲、等调用返回才派发，而 ``Page.navigate`` 的响应又要等文档请求放行，
        于是双方互等，整个 :meth:`open` 死等到超时。真机上撞过。

        :param types: 按 CDP ``ResourceType`` 屏蔽（``Image`` / ``Media`` / ``Font`` /
            ``Stylesheet`` / ``Script`` / ``XHR`` …）
        :param url_patterns: 按 URL 屏蔽，:mod:`fnmatch` 通配（``"*://ads.*"``）。
            和 ``types`` 是**或**关系
        :returns: 上下文管理器，产出 :class:`~sleight.core.resources.BlockStats`。
            退出后仍然可读
        :raises ValueError: 两个都没给，或 ``types`` 里有不认识的 ResourceType
        """
        kinds = frozenset(types or ())
        patterns = tuple(url_patterns or ())
        if not kinds and not patterns:
            raise ValueError("block() needs types= or url_patterns=; blocking nothing is a no-op")
        if unknown := sorted(kinds - RESOURCE_TYPES):
            raise ValueError(
                f"unknown ResourceType(s) {unknown}. Valid values are {sorted(RESOURCE_TYPES)}"
            )

        stats = BlockStats()

        def decide(ev: Event) -> bool:
            if ev.method != "Fetch.requestPaused" or ev.session_id != self._sid:
                return False
            kind = ev.params.get("resourceType", "Other")
            url = (ev.params.get("request") or {}).get("url", "")
            hit = kind in kinds or any(fnmatch(url, p) for p in patterns)
            # 只能 fire-and-forget：这个回调跑在 recv 的调用栈里，call() 会递归读同一个
            # socket。quiet=True 是因为请求可能在我们回应之前就被浏览器自己取消了 ——
            # 那时候报的 Invalid InterceptionId 是正常现象，不该炸掉一段无关的动作
            self._t.send_no_wait(
                "Fetch.failRequest" if hit else "Fetch.continueRequest",
                {"requestId": ev.params["requestId"],
                 **({"errorReason": "BlockedByClient"} if hit else {})},
                session_id=self._sid,
                quiet=True,
            )
            stats._count(kind, blocked=hit)
            return True

        self.call("Fetch.enable", {"patterns": [{"urlPattern": "*"}]})
        try:
            with self._t.urgent_events(decide):
                yield stats
        finally:
            # disable 会放行所有还挂着的请求 —— 中途抛异常也不会把页面卡死
            with contextlib.suppress(SleightError):
                self.call("Fetch.disable")

    def cookies(self, urls: Iterable[str] | None = None) -> list[dict[str, Any]]:
        """Cookie。需要构造时 ``track_network=True``（默认）。

        :param urls: 只要这些 URL 能看到的 cookie。``None`` = 当前页面可见的那些。
            查"某个 origin 下有哪些 cookie"用这个，不用先导航过去
        :returns: CDP 的 ``Cookie`` 字典列表
        """
        params = {} if urls is None else {"urls": list(urls)}
        return self.call("Network.getCookies", params).get("cookies", [])

    def clear_site_data(
        self, origin: str, *, types: Iterable[StorageType | str] | None = None
    ) -> ClearReport:
        """清掉一个 origin 的站点数据。

            >>> s.clear_site_data("https://www.example.com")
            >>> s.clear_site_data("https://www.example.com", types=[StorageType.COOKIES])

        **按 origin 收敛是安全默认值。** 全局清理会把别的站点的登录态一起端掉 ——
        依赖插件（付费墙解锁一类）的场景下这是致命的。要全清用
        :meth:`clear_browser_data`，那必须是显式动作。

        **默认清的不止 cookie。** 只清 cookie 会被 ``localStorage`` / ``indexedDB``
        里的副本立刻还原 —— 反检测服务的设备标识不只落在 cookie 上。默认这五类：
        ``cookies`` / ``local_storage`` / ``indexeddb`` / ``cache_storage`` /
        ``service_workers``。

        :param origin: ``https://host[:port]``。带了 path / query 会被归一化掉
        :param types: 要清哪几类，见 :class:`~sleight.core.types.StorageType`。
            ``None`` = 上面那五类
        :returns: :class:`~sleight.core.types.ClearReport` —— 清掉了哪些 cookie、
            占用字节前后各是多少。CDP 的清理命令什么都不返回，这份账是这里现测的
        :raises ValueError: ``origin`` 缺 scheme 或 host，或 ``types`` 里有不认识的值
        """
        origin = _normalize_origin(origin)
        wanted = tuple(
            StorageType(t) for t in (types if types is not None else _DEFAULT_CLEAR_TYPES)
        )
        if not wanted:
            raise ValueError("clear_site_data() needs at least one storage type")

        before = self._origin_cookies(origin)
        usage_before = self._origin_usage(origin)

        self.call("Storage.clearDataForOrigin", {
            "origin": origin, "storageTypes": ",".join(t.value for t in wanted)
        })

        after = self._origin_cookies(origin)
        gone: tuple[str, ...] | None = None
        if before is not None and after is not None:
            survivors = {c.get("name") for c in after}
            gone = tuple(str(c.get("name")) for c in before if c.get("name") not in survivors)

        return ClearReport(
            origin=origin,
            types=wanted,
            cookies=gone,
            usage_before=usage_before,
            usage_after=self._origin_usage(origin),
        )

    def clear_browser_data(self, *, cookies: bool = True, cache: bool = True) -> None:
        """浏览器级全清。

        ⚠️ **会掉所有站点的登录态** —— 包括你没想动的那些。想只清一个站点用
        :meth:`clear_site_data`，那才是安全默认值。

        :param cookies: 清 ``Network.clearBrowserCookies``
        :param cache: 清 ``Network.clearBrowserCache``
        """
        if cookies:
            self.call("Network.clearBrowserCookies")
        if cache:
            self.call("Network.clearBrowserCache")

    def _origin_cookies(self, origin: str) -> list[dict[str, Any]] | None:
        """``None`` = 没测成（Network domain 没开），区别于"一个都没有"。"""
        try:
            return self.cookies(urls=[origin])
        except (ProtocolError, SleightError):
            log.debug("could not read cookies for %s", origin, exc_info=True)
            return None

    def _origin_usage(self, origin: str) -> int:
        try:
            return int(self.call(
                "Storage.getUsageAndQuota", {"origin": origin}
            ).get("usage", 0))
        except (ProtocolError, SleightError):
            log.debug("could not read storage usage for %s", origin, exc_info=True)
            return 0

    def close(self) -> None:
        """关闭会话。幂等，可以重复调。

        自有 target 走 ``Target.closeTarget``（断 WebSocket 不等于关 tab，不关就是
        泄漏）；接管的只 ``Target.detachFromTarget``，那是别人的页面。

        关闭过程中的错误只记 debug 日志，不往外抛 —— 免得盖住调用方原本的异常。
        """
        if self._closed:
            return
        self._closed = True
        try:
            if self._owned:
                self._t.call("Target.closeTarget", {"targetId": self._target_id})
            else:
                self._t.call("Target.detachFromTarget", {"sessionId": self._sid})
        except SleightError:
            log.debug("error while closing session %r", self, exc_info=True)
