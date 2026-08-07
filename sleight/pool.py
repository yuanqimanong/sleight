"""Pool 与 InstanceHandle。

Pool 是**唯一**的实例选择者，并且**全生命周期持有同一个 LeaseStore** —— 这是排他性
的命脉，锁表绝不能归属单次 ``lease()`` 调用。
"""

from __future__ import annotations

import logging
import math
import random
import threading
import time
import weakref
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from itertools import count
from typing import Any

from .core.errors import Busy, LeaseStillHeld, NotFound, SleightError, TimeoutError
from .core.session import Session
from .core.transport import Transport
from .core.types import InstanceInfo
from .lease.base import Lease, LeaseHandle
from .lease.memory import MemoryLease
from .providers.base import Provider

log = logging.getLogger("sleight.pool")

__all__ = ["BrowserContext", "InstanceHandle", "Pool"]

DEFAULT_TTL = 30.0
DEFAULT_DISCOVERY_TTL = 5.0
BACKOFF_BASE = 1.0
BACKOFF_MAX = 60.0
RETRY_MIN = 0.05
RETRY_MAX = 1.0
#: 「匹配不上」的报错里最多列几个实例。全列出来在几百个 profile 的池子里没法读
_NAMES_IN_ERRORS = 12

Predicate = Callable[[InstanceInfo], bool]


# --------------------------------------------------------------------------- #
# 续租
# --------------------------------------------------------------------------- #


class _Renewer:
    """后台续租。周期 = ttl/3。

    续租失败立刻置 ``lease_lost`` **并关闭 WebSocket** —— 把 TOCTOU 窗口从"TTL 长度"
    压到"一次续租周期"。连接关了就发不出命令，比检查标志位可靠。

    窗口无法归零。这是协作式租约的固有性质。
    """

    __slots__ = ("_handle_ref", "_lease", "_stop", "_thread")

    def __init__(self, lease: LeaseHandle, handle: InstanceHandle) -> None:
        self._lease = lease
        # **弱引用**，不能直接持有 handle 的绑定方法。运行中的线程被 threading 强引用，
        # 若线程再强引用 handle，一个忘了 close() 的 handle 就永远不会被回收，续租线程
        # 无限续下去 —— TTL 这道兜底就彻底失效了（前提是"持有者消失后租约会过期"）。
        self._handle_ref: weakref.ReferenceType[InstanceHandle] = weakref.ref(handle)
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name=f"sleight-renew-{lease.key}", daemon=True
        )
        self._thread.start()

    def _run(self) -> None:
        period = max(self._lease.ttl / 3.0, 1.0)
        while not self._stop.wait(period):
            if self._handle_ref() is None:
                # handle 被丢弃了，停止续租，让租约在一个 TTL 后自然过期
                log.debug("lease %s: owner was garbage-collected, stopping renewal",
                          self._lease.key)
                return
            if not self._lease.renew():
                log.error("lease %s lost; severing the connection", self._lease.key)
                if (handle := self._handle_ref()) is not None:
                    try:
                        handle._sever()
                    except Exception:
                        log.debug("sever handler failed", exc_info=True)
                return

    def stop(self) -> None:
        self._stop.set()


# --------------------------------------------------------------------------- #
# InstanceHandle
# --------------------------------------------------------------------------- #


class BrowserContext:
    """一个隔离的 browser context：独立 cookie jar、独立 storage、**独立 socket pool**。

    最后那条是 antidetect 场景里最值钱的副作用，而从名字上完全看不出来。链路是这样的：
    隧道**按 TCP 连接**分配出口，Chrome 又会复用 keep-alive 连接，于是一整轮采集全压在
    同一个出口 IP 上，几十篇之后必被风控。新建 context 会让到隧道的连接**必然重建**，
    出口跟着换。

    清缓存、URL 挂唯一查询参数、拉长间隔、``Network.emulateNetworkConditions`` 切
    offline —— 这些**都不管用**，原因见文档"如何强制换出口"。

    ⚠️ **上下文里用不了扩展。** 这里建的是 off-the-record（无痕）上下文，而 Chrome
    默认不在无痕里启用扩展 —— 实测 ``chrome-extension://<id>/...`` 在默认 context 打得开、
    在这里是 ``net::ERR_BLOCKED_BY_CLIENT``。所以"换出口"和"用插件"目前互斥：依赖插件
    的采集要靠多开 profile + 各配各的代理来换出口，不能靠 context。

    别自己 ``new`` 它，用
    :meth:`InstanceHandle.context() <sleight.pool.InstanceHandle.context>`。

    :param handle: 宿主 handle
    :param context_id: ``Target.createBrowserContext`` 返回的 id
    """

    __slots__ = ("_closed", "_handle", "_sessions", "context_id")

    def __init__(self, handle: InstanceHandle, context_id: str) -> None:
        self._handle = handle
        self.context_id = context_id
        self._sessions: list[Session] = []
        self._closed = False

    def __enter__(self) -> BrowserContext:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def __repr__(self) -> str:
        return f"<BrowserContext {self.context_id[:8]}{' closed' if self._closed else ''}>"

    @property
    def closed(self) -> bool:
        return self._closed

    def session(self, **kw: Any) -> Session:
        """在这个 context 里新建一个自有 tab。

        :param kw: 透传给 :class:`~sleight.core.session.Session`
        :returns: 新会话。context 关闭时会连带关掉它
        :raises SleightError: context 已经销毁了
        """
        if self._closed:
            raise SleightError(f"{self!r} is disposed; create a new context")
        s = Session.create(self._handle.transport, browser_context_id=self.context_id, **kw)
        self._sessions.append(s)
        # handle 也记一份：用户忘了关 context 时，handle.close() 仍然收得掉这些 tab
        self._handle._sessions.append(s)
        return s

    def exit_ip(self, **kw: Any) -> str:
        """本 context 走出去的公网 IP。开一个临时 tab 去问，问完关掉。

        用来验证"新 context 真的换了出口" —— 这是 :class:`BrowserContext` 存在的
        主要理由，也是唯一能直接观测它的方式。

        :param kw: 透传给 :meth:`Session.exit_ip() <sleight.core.session.Session.exit_ip>`
        :returns: IP 字符串
        """
        with self.session() as s:
            return s.exit_ip(**kw)

    def close(self) -> None:
        """关掉本 context 的会话，然后销毁 context。幂等。

        三步各自独立 try —— 手写时漏掉任何一步都会在浏览器里攒孤儿 target/context，
        而攒出来的东西在下一次 ``Target.getTargets`` 之前完全看不见。这正是这个类
        存在的理由。

        :raises ExceptionGroup: 拆卸过程中出过错。**context 一定已经销毁**
        """
        if self._closed:
            return
        self._closed = True
        errors: list[BaseException] = []

        for s in self._sessions:
            if not s.closed:
                _swallow(errors, s.close)
        self._sessions.clear()

        _swallow(errors, lambda: self._handle.transport.call(
            "Target.disposeBrowserContext", {"browserContextId": self.context_id}
        ))
        if errors:
            raise ExceptionGroup(f"errors while disposing {self!r}", errors)


class InstanceHandle:
    """一次租用。

    关闭顺序是硬性的，任何一步失败都继续走完后面的（best-effort），最后汇总异常::

        1. 关闭本 handle 创建的所有 Session（各自关掉自有 target）
        2. 停止续租
        3. 关闭 WebSocket
        4. provider.release()
        5. lease.release()          ← 必须最后

    :param info: 租到的实例
    :param provider: 它属于哪个 provider
    :param lease: 已经拿到手的租约句柄
    :param strict: True 时，close() 发现还有没关的 Session 会在拆卸完成后抛
        :class:`~sleight.core.errors.LeaseStillHeld`；默认只记一条 warning
    """

    def __init__(
        self,
        info: InstanceInfo,
        provider: Provider,
        lease: LeaseHandle,
        *,
        strict: bool = False,
    ) -> None:
        self.info = info
        self.provider = provider
        self.lease = lease
        self._strict = strict
        self._transport: Transport | None = None
        self._sessions: list[Session] = []
        self._contexts: list[BrowserContext] = []
        self._closed = False
        self._renewer = _Renewer(lease, self)
        # handle 被丢弃（忘了 close / with）时至少停掉续租线程，让租约按 TTL 过期。
        # 这不能替代 close()：连接和 target 仍会留到进程结束，但租约不会被永远续下去。
        self._finalizer = weakref.finalize(self, self._renewer.stop)

    def __enter__(self) -> InstanceHandle:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def __repr__(self) -> str:
        return f"<InstanceHandle {self.info.uid}{' closed' if self._closed else ''}>"

    # ------------------------------------------------------------------ #

    @property
    def transport(self) -> Transport:
        """懒建的浏览器级 WS 连接。一个 handle 一条，多个 Session 共用。

        :raises LeaseLost: 租约已经丢了，这个 handle 是死的
        """
        if self.lease.lost:
            from .core.errors import LeaseLost

            raise LeaseLost(f"lease {self.lease.key} was lost; this handle is dead")
        if self._transport is None or self._transport.closed:
            ep = self.provider.endpoint(self.info.id)
            self._transport = Transport.connect(ep.ws_url, headers=dict(ep.headers))
        return self._transport

    def session(self, **kw: Any) -> Session:
        """在这个实例上新建一个**自有 tab**。

        落在默认 browser context 里，Cookie 和登录态照样继承。

        :param kw: 透传给 :class:`~sleight.core.session.Session` ——
            ``human`` / ``rng`` / ``track_network``
        :returns: 新会话。handle 关闭时会连带关掉它
        """
        s = Session.create(self.transport, **kw)
        self._sessions.append(s)
        return s

    def attach(self, target_id: str, **kw: Any) -> Session:
        """接管这个实例上已有的一个 tab。

        :param target_id: 从 :meth:`targets` 拿
        :param kw: 透传给 :class:`~sleight.core.session.Session`
        :returns: 新会话。handle 关闭时只 detach，不关这个 tab
        """
        s = Session.attach(self.transport, target_id, **kw)
        self._sessions.append(s)
        return s

    def context(
        self,
        *,
        proxy: str | None = None,
        proxy_bypass: str | None = None,
        dispose_on_detach: bool = True,
    ) -> BrowserContext:
        """新建一个隔离的 browser context。

            >>> with inst.context() as ctx, ctx.session(human=True) as s:
            ...     s.open(url)              # 独立 cookie、独立 socket pool

        **不传 ``proxy`` 就继承进程级的 ``--proxy-server``，而且这样已经足够换出口** ——
        换出口靠的是新建 socket pool，不是换代理地址。这一条能省掉一整天的排查，
        细节见 :class:`BrowserContext`。

        :param proxy: 本 context 专用的代理，映射 CDP 的 ``proxyServer``。
            ``None`` = 继承进程级代理
        :param proxy_bypass: 绕过代理的地址列表，映射 ``proxyBypassList``。
            只在给了 ``proxy`` 时有意义
        :param dispose_on_detach: 调试连接断开时让浏览器自己销毁这个 context。
            默认开着，是 :meth:`BrowserContext.close` 之外的第二道保险 —— 进程被
            kill -9 时 ``close()`` 根本没机会跑
        :returns: :class:`BrowserContext`。用 ``with``，或者自己 ``close()``
        """
        params: dict[str, Any] = {"disposeOnDetach": dispose_on_detach}
        if proxy is not None:
            params["proxyServer"] = proxy
        if proxy_bypass is not None:
            params["proxyBypassList"] = proxy_bypass
        cid = self.transport.call("Target.createBrowserContext", params)["browserContextId"]
        ctx = BrowserContext(self, cid)
        self._contexts.append(ctx)
        return ctx

    def exit_ip(self, **kw: Any) -> str:
        """这个实例走出去的公网 IP。开一个临时 tab 去问，问完关掉。

        被拦时的第一线索。注意默认 context 下**整轮采集都是同一个出口** —— 换出口要
        用 :meth:`context`，理由见 :class:`BrowserContext`。

        :param kw: 透传给 :meth:`Session.exit_ip() <sleight.core.session.Session.exit_ip>`
        :returns: IP 字符串
        """
        with self.session() as s:
            return s.exit_ip(**kw)

    def targets(self) -> list[dict[str, Any]]:
        """浏览器里现有的 page target，给 :meth:`attach` 挑。

        :returns: CDP 的 ``TargetInfo`` 字典列表，只保留 ``type == "page"`` 的
        """
        infos = self.transport.call("Target.getTargets")["targetInfos"]
        return [t for t in infos if t.get("type") == "page"]

    # ------------------------------------------------------------------ #

    def _sever(self) -> None:
        """续租失败时从后台线程调用：立刻掐断连接。

        标记成 ``severed``，这样 owner 线程拿到的是 :class:`LeaseLost` 而不是一条
        和隧道抖动没法区分的 ``ConnectionError``。
        """
        if (t := self._transport) is not None:
            t.close(severed=True)

    def close(self) -> None:
        """按固定顺序拆卸并释放租约。幂等。

        任何一步失败都继续走完后面的，最后把异常汇总成 :class:`ExceptionGroup`。

        :raises LeaseStillHeld: ``strict=True`` 且还有没关的 Session。注意租约**已经**
            释放了，抛这个只是为了让你知道有代码路径漏了 ``close()``
        :raises ExceptionGroup: 拆卸过程中出过错
        """
        if self._closed:
            return
        self._closed = True
        errors: list[BaseException] = []

        # 用户自己 close 掉的 Session 不算泄漏
        self._sessions = [s for s in self._sessions if not s.closed]

        # 严格模式**先记账、后抛出**。原来在这里直接 raise，而 _closed 已经置位，
        # 于是五步拆卸一步都不做、重试又被幂等守卫挡掉 —— 租约、连接、Session、
        # 续租线程全部永久泄漏，恰恰是这个检查想防的事情的放大版。
        leaked = len(self._sessions)
        if leaked:
            log.warning("%d session(s) still open on %s; closing them", leaked, self.info.uid)

        for s in self._sessions:
            _swallow(errors, s.close)
        self._sessions.clear()

        # context 要在 transport 关掉之前销毁 —— 之后就没有连接可以发这条命令了。
        # disposeOnDetach 是兜底，但那要等浏览器自己发现连接断了
        for ctx in self._contexts:
            _swallow(errors, ctx.close)
        self._contexts.clear()

        _swallow(errors, self._renewer.stop)
        self._finalizer.detach()
        if self._transport is not None:
            _swallow(errors, self._transport.close)
        _swallow(errors, lambda: self.provider.release(self.info.id))
        _swallow(errors, self.lease.release)          # 必须最后

        if leaked and self._strict:
            raise LeaseStillHeld(
                f"{leaked} session(s) were still open on {self.info.uid} "
                "(they have been closed and the lease released)"
            )
        if errors:
            raise ExceptionGroup(f"errors while closing {self.info.uid}", errors)


def _swallow(sink: list[BaseException], fn: Callable[[], Any]) -> None:
    try:
        fn()
    except BaseException as exc:
        sink.append(exc)


def _select(
    items: list[InstanceInfo],
    where: Predicate | None,
    name: str | None,
    names: frozenset[str] | None,
    instance_id: str | None,
    provider: str | None,
) -> list[InstanceInfo]:
    """只应用**身份**筛选。状态筛选（``ready_only``）不在这里 —— 见 ``_candidates``。"""
    if instance_id is not None:
        items = [i for i in items if i.id == instance_id or i.uid == instance_id]
    if name is not None:
        items = [i for i in items if i.name == name]
    if names is not None:
        items = [i for i in items if i.name in names]
    if provider is not None:
        items = [i for i in items if i.provider == provider]
    if where is not None:
        items = [i for i in items if where(i)]
    return items


def _no_match(
    seen: list[InstanceInfo],
    where: Predicate | None,
    name: str | None,
    names: frozenset[str] | None,
    instance_id: str | None,
    provider: str | None,
) -> str:
    """匹配不上时的报错。**必须把看得见的名字列出来** —— 这类错九成是名字打错了，
    而"没匹配上"本身不带任何能改的信息。"""
    asked = []
    if instance_id is not None:
        asked.append(f"instance_id={instance_id!r}")
    if name is not None:
        asked.append(f"name={name!r}")
    if names is not None:
        asked.append(f"names={sorted(names)!r}")
    if provider is not None:
        asked.append(f"provider={provider!r}")
    if where is not None:
        asked.append("where=<predicate>")

    if not seen:
        return f"no instance matches {' '.join(asked) or '<no filter>'}; the pool is empty"
    shown = [i.uid + (f" ({i.name})" if i.name else "") for i in seen[:_NAMES_IN_ERRORS]]
    more = f", +{len(seen) - _NAMES_IN_ERRORS} more" if len(seen) > _NAMES_IN_ERRORS else ""
    return (
        f"no instance matches {' '.join(asked) or '<no filter>'}. "
        f"{len(seen)} visible: {', '.join(shown)}{more}"
    )


# --------------------------------------------------------------------------- #
# Pool
# --------------------------------------------------------------------------- #


class Pool:
    def __init__(
        self,
        providers: Sequence[Provider],
        *,
        lease: Lease | None = None,
        namespace: str = "sleight",
        ttl: float = DEFAULT_TTL,
        discovery_ttl: float = DEFAULT_DISCOVERY_TTL,
        strict_close: bool = False,
    ) -> None:
        """聚合若干 provider，成为唯一的实例选择者。

        :param providers: 至少一个。``name`` 在池内**必须唯一** —— 租约 key 靠它
            保证全局不撞车
        :param lease: 锁表后端。``None`` 用进程内的
            :class:`~sleight.lease.memory.MemoryLease`；跨进程共享同一批实例时
            换成 redis 后端
        :param namespace: 租约 key 前缀，用来隔离多套环境（prod / staging）
        :param ttl: 租约存活秒数。后台线程按 ``ttl/3`` 续租
        :param discovery_ttl: ``list_instances()`` 结果的缓存秒数
        :param strict_close: 传给每个 :class:`InstanceHandle`
        :raises ValueError: 没给 provider，或 ``name`` 重复
        """
        if not providers:
            raise ValueError("Pool needs at least one provider")
        names = [p.name for p in providers]
        if len(set(names)) != len(names):
            dupes = sorted({n for n in names if names.count(n) > 1})
            raise ValueError(
                f"provider names must be unique within a Pool (uid depends on them); "
                f"duplicates: {dupes}"
            )

        self.providers = list(providers)
        self.namespace = namespace
        self.ttl = ttl
        self.discovery_ttl = discovery_ttl
        self.strict_close = strict_close

        # 长期持有 —— 绝不在 lease() 里创建，那会让各线程拿到不同的锁表
        self._lease: Lease = lease if lease is not None else MemoryLease()

        self._cache: list[InstanceInfo] = []
        # **不能用 0.0 当"还没拉过"的哨兵。** time.monotonic() 在 Linux 上是**开机以来**
        # 的秒数：刚起来的容器 / CI runner 上它可能只有 20，于是 discover() 里
        # `now - 0.0 < discovery_ttl` 成立，第一次调用就直接返回空缓存、provider 一次
        # 都不会被问到 —— 表现是进程启动后的头一分钟内 lease() 找不到任何实例。
        # 开发机开机久了永远碰不到，CI 上必现。
        self._cache_at = -math.inf
        self._cache_lock = threading.Lock()
        self._failures: dict[str, tuple[int, float]] = {}     # name -> (次数, 下次可试时间)
        self._cursor = count()
        self._executor: ThreadPoolExecutor | None = None

    def __repr__(self) -> str:
        return f"<Pool {[p.name for p in self.providers]} lease={type(self._lease).__name__}>"

    def close(self) -> None:
        """关掉发现用的线程池。不影响已经发出去的租约。"""
        if self._executor is not None:
            self._executor.shutdown(wait=False)
            self._executor = None

    # ------------------------------------------------------------------ #
    # 租用
    # ------------------------------------------------------------------ #

    def lease(
        self,
        *,
        where: Predicate | None = None,
        name: str | None = None,
        names: Iterable[str] | None = None,
        instance_id: str | None = None,
        provider: str | None = None,
        block: bool = True,
        timeout: float | None = None,
        ready_only: bool = False,
    ) -> InstanceHandle:
        """拿一个空闲实例，租约到手后返回。

        默认**阻塞**等待 —— 否则每个调用点都得自己写 ``while + sleep``，线程池写法
        根本不成立。选择策略是 first-free（天然 work-stealing），起始下标按 provider
        打散，免得总把第一个 Manager 塞满。

        **「都被占着」和「根本没有这个实例」是两回事。** 筛选条件在当前实例列表里
        一个都匹配不上时立刻抛 :class:`~sleight.core.errors.NotFound` 并列出看得见的
        名字 —— 名字打错了却阻塞到 timeout，从 ``TimeoutError`` 里完全看不出是配错了。
        唯一的例外是 ``ready_only``：它筛的是状态不是身份，等下去有意义。

        :param where: 作用在 :class:`~sleight.core.types.InstanceInfo` 上的谓词。
            和 ``name`` / ``names`` / ``provider`` 是**与**关系
        :param name: 按 ``info.name`` 精确匹配。与 ``names`` 互斥
        :param names: 按 ``info.name`` 匹配其中任意一个。与 ``name`` 互斥
        :param instance_id: 指定实例。裸 id 和 ``"provider:id"`` 形式的 uid 都收
        :param provider: 只在这个 provider 里挑
        :param block: False 表示拿不到立刻抛 :class:`~sleight.core.errors.Busy`
        :param timeout: 阻塞上限，秒。``None`` 表示一直等
        :param ready_only: True 表示跳过没在运行的实例。默认参与调度，租到之后由
            ``ensure_ready()`` 拉起
        :returns: :class:`InstanceHandle`，用 ``with`` 包起来
        :raises ValueError: ``name`` 和 ``names`` 一起给了
        :raises Busy: ``block=False`` 且没有空闲实例
        :raises TimeoutError: 超过 ``timeout`` 还没拿到
        :raises NotFound: 筛选条件在池里一个实例都匹配不上
        """
        if name is not None and names is not None:
            raise ValueError("lease() takes name= or names=, not both")
        deadline = None if timeout is None else time.monotonic() + timeout
        backoff = RETRY_MIN

        while True:
            for info in self._candidates(where, name, names, instance_id, provider, ready_only):
                handle = self._try_acquire(info)
                if handle is not None:
                    return handle

            if instance_id is not None and not block:
                raise Busy(f"instance {instance_id!r} is leased by someone else")
            if not block:
                raise Busy("no free instance available")
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError(f"no free instance within {timeout}s")

            # 带抖动的退避，避免多进程惊群
            nap = min(backoff, BACKOFF_MAX) * (0.5 + random.random())
            if deadline is not None:
                nap = min(nap, max(deadline - time.monotonic(), 0.0))
            time.sleep(nap)
            backoff = min(backoff * 2, RETRY_MAX)

    def lease_many(
        self, n: int, *, timeout: float | None = None, **kw: Any
    ) -> list[InstanceHandle]:
        """一次租 ``n`` 个实例，用于并发。

            >>> with ExitStack() as stack:
            ...     handles = pool.lease_many(4, names=["a", "b", "c", "d"])
            ...     for h in handles:
            ...         stack.enter_context(h)

        **部分失败会整体回滚** —— 拿到 3 个第 4 个超时，那 3 个会被还回去再抛。自己写
        循环最容易漏的就是这一步，漏了就是三个实例被占满一个 TTL。

        ``timeout`` 是**这一整批**的上限，不是每个的 —— 每个都给 30 s 的话，一批 8 个
        最坏要等 4 分钟，而调用方以为自己写的是 30 秒。

        :param n: 要几个。必须 ≥ 1
        :param timeout: 整批的阻塞上限，秒。``None`` 表示一直等
        :param kw: 透传给 :meth:`lease`（``where`` / ``name`` / ``names`` /
            ``provider`` / ``block`` / ``ready_only``）
        :returns: ``n`` 个 :class:`InstanceHandle`，按拿到的顺序
        :raises ValueError: ``n < 1``，或 ``kw`` 里带了 ``instance_id``（租不到两个）
        :raises Busy: ``block=False`` 且空闲实例不够 ``n`` 个
        :raises TimeoutError: 整批没能在 ``timeout`` 内凑齐
        """
        if n < 1:
            raise ValueError(f"lease_many() needs n >= 1, got {n}")
        if kw.get("instance_id") is not None:
            raise ValueError("lease_many() cannot use instance_id= — it names a single instance")

        deadline = None if timeout is None else time.monotonic() + timeout
        handles: list[InstanceHandle] = []
        try:
            for _ in range(n):
                left = None if deadline is None else max(deadline - time.monotonic(), 0.0)
                handles.append(self.lease(timeout=left, **kw))
        except BaseException:
            for h in handles:
                try:
                    h.close()
                except BaseException:
                    log.warning("could not roll back %s", h.info.uid, exc_info=True)
            raise
        return handles

    def _try_acquire(self, info: InstanceInfo) -> InstanceHandle | None:
        key = f"{self.namespace}:{info.uid}"
        token = self._lease.acquire(key, ttl=self.ttl)
        if token is None:
            return None

        handle_lease = LeaseHandle(self._lease, key, token, self.ttl)
        provider = self._provider(info.provider)
        try:
            provider.ensure_ready(info.id)
            return InstanceHandle(info, provider, handle_lease, strict=self.strict_close)
        except SleightError:
            log.warning("%s not ready, skipping", info.uid, exc_info=True)
            handle_lease.release()
            return None
        except BaseException:
            # 任何**非** SleightError 也必须还锁。原来只兜 SleightError，于是一个
            # provider 的 AttributeError / KeyboardInterrupt 就把租约搁置整整一个 TTL，
            # 而且没有 InstanceHandle 存在、没人能释放它。
            handle_lease.release()
            raise

    # ------------------------------------------------------------------ #
    # 发现
    # ------------------------------------------------------------------ #

    def _candidates(
        self,
        where: Predicate | None,
        name: str | None,
        names: Iterable[str] | None,
        instance_id: str | None,
        provider: str | None,
        ready_only: bool,
    ) -> list[InstanceInfo]:
        wanted = None if names is None else frozenset(names)
        seen = self.discover()
        items = _select(seen, where, name, wanted, instance_id, provider)
        if not items:
            # 缓存最长 discovery_ttl 秒是陈的。刚建出来的 profile 会被误判成"没有
            # 这个实例" —— 报 NotFound 之前必须再问一次源头
            seen = self.discover(force=True)
            items = _select(seen, where, name, wanted, instance_id, provider)
        if not items:
            raise NotFound(_no_match(seen, where, name, wanted, instance_id, provider))

        # ready_only 单独放在 NotFound 判定之后：它筛的是**状态**不是身份，实例这会儿
        # 没起来不代表它不存在，等下去是有意义的
        if ready_only:
            items = [i for i in items if i.ready]

        # 打散起始下标，否则 first-free 会一直把第一个 Manager 塞满。只改变平级空闲
        # 实例的尝试顺序，不改变 first-free 语义。
        #
        # 用 itertools.count 而不是 `self._cursor = (self._cursor + 1) % n`：后者是
        # 无锁的读-改-写，并发 lease() 会算出同一个起点，于是所有线程先撞同一个实例 ——
        # 恰好是打散想避免的。`next()` 在 CPython 里是原子的。
        if items:
            offset = next(self._cursor) % len(items)
            items = items[offset:] + items[:offset]
        return items

    def discover(self, *, force: bool = False) -> list[InstanceInfo]:
        """并发拉取所有 provider 的实例，带 TTL 缓存。

        单个 provider 超时或抛异常 → 本轮跳过并记 warning，**不阻塞全池**；
        连续失败进指数 backoff（上限 60 s）。

        :param force: 忽略缓存，强制重新拉
        :returns: 所有可见实例
        """
        with self._cache_lock:
            if not force and (time.monotonic() - self._cache_at) < self.discovery_ttl:
                return self._cache

            now = time.monotonic()
            live = [p for p in self.providers if self._failures.get(p.name, (0, 0.0))[1] <= now]
            skipped = len(self.providers) - len(live)
            if skipped:
                log.debug("skipping %d provider(s) in backoff", skipped)

            results: list[InstanceInfo] = []
            for provider, outcome in zip(live, self._fetch_all(live), strict=True):
                if isinstance(outcome, BaseException):
                    self._note_failure(provider.name, outcome)
                else:
                    self._failures.pop(provider.name, None)
                    results.extend(outcome)

            self._cache = results
            self._cache_at = time.monotonic()
            return results

    def _fetch_all(self, providers: list[Provider]) -> list[list[InstanceInfo] | BaseException]:
        if len(providers) == 1:
            try:
                return [providers[0].list_instances()]
            except BaseException as exc:
                return [exc]

        if self._executor is None:
            self._executor = ThreadPoolExecutor(
                max_workers=min(8, len(self.providers)), thread_name_prefix="sleight-discover"
            )
        futures = [self._executor.submit(p.list_instances) for p in providers]
        out: list[list[InstanceInfo] | BaseException] = []
        for f in futures:
            try:
                out.append(f.result())
            except BaseException as exc:
                out.append(exc)
        return out

    def _note_failure(self, name: str, exc: BaseException) -> None:
        count = self._failures.get(name, (0, 0.0))[0] + 1
        delay = min(BACKOFF_BASE * (2 ** (count - 1)), BACKOFF_MAX)
        self._failures[name] = (count, time.monotonic() + delay)
        log.warning(
            "provider %r discovery failed (%d in a row), backing off %.0fs: %s",
            name, count, delay, exc,
        )

    def _provider(self, name: str) -> Provider:
        for p in self.providers:
            if p.name == name:
                return p
        raise NotFound(f"no provider named {name!r} in this pool")

    # ------------------------------------------------------------------ #

    def instances(self) -> Iterable[InstanceInfo]:
        """当前可见的全部实例（走缓存）。"""
        return self.discover()
