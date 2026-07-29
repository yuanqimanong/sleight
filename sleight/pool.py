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

__all__ = ["InstanceHandle", "Pool"]

DEFAULT_TTL = 30.0
DEFAULT_DISCOVERY_TTL = 5.0
BACKOFF_BASE = 1.0
BACKOFF_MAX = 60.0
RETRY_MIN = 0.05
RETRY_MAX = 1.0

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

        :param where: 作用在 :class:`~sleight.core.types.InstanceInfo` 上的谓词。
            按名字挑用 ``lambda i: i.name == "Win-US-02"``，按标签挑用
            ``lambda i: "us" in i.tags``
        :param instance_id: 指定实例。裸 id 和 ``"provider:id"`` 形式的 uid 都收
        :param provider: 只在这个 provider 里挑
        :param block: False 表示拿不到立刻抛 :class:`~sleight.core.errors.Busy`
        :param timeout: 阻塞上限，秒。``None`` 表示一直等
        :param ready_only: True 表示跳过没在运行的实例。默认参与调度，租到之后由
            ``ensure_ready()`` 拉起
        :returns: :class:`InstanceHandle`，用 ``with`` 包起来
        :raises Busy: ``block=False`` 且没有空闲实例
        :raises TimeoutError: 超过 ``timeout`` 还没拿到
        :raises NotFound: 指定的 ``instance_id`` 在这个池里根本不存在
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        backoff = RETRY_MIN

        while True:
            for info in self._candidates(where, instance_id, provider, ready_only):
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
        instance_id: str | None,
        provider: str | None,
        ready_only: bool,
    ) -> list[InstanceInfo]:
        items = self.discover()
        if instance_id is not None:
            matches = [i for i in items if i.id == instance_id or i.uid == instance_id]
            if not matches:
                # 缓存最长 discovery_ttl 秒是陈的。刚建出来的 profile 会被误判成
                # NotFound —— 而 NotFound 只能由 status() 给出。
                items = self.discover(force=True)
                matches = [i for i in items if i.id == instance_id or i.uid == instance_id]
            if not matches:
                raise NotFound(f"no instance {instance_id!r} in this pool")
            items = matches
        if provider is not None:
            items = [i for i in items if i.provider == provider]
        if ready_only:
            items = [i for i in items if i.ready]
        if where is not None:
            items = [i for i in items if where(i)]

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
