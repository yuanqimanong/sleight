"""排他性 —— 这个库最重要的一条不变量。

如果 ``Provider.lease()`` 每次都新建 Pool（各线程持有不同锁表），这些测试必挂。
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict

import pytest

from sleight import Pool
from sleight.core.errors import Busy, TimeoutError
from sleight.lease.memory import MemoryLease

from .conftest import FakeProvider, run_threads


def test_provider_pool_is_a_singleton():
    """mgr.lease() 的排他性完全依赖这一点。"""
    p = FakeProvider()
    pools = []
    run_threads(lambda: pools.append(p.pool), n=16)
    assert len({id(x) for x in pools}) == 1, "Provider.pool must be a lazily-created singleton"


def test_lease_is_exclusive_across_threads():
    """20 线程争抢 3 个实例，任一时刻同一 uid 至多一个持有者。"""
    provider = FakeProvider(n=3)
    live: dict[str, int] = defaultdict(int)
    lock = threading.Lock()
    violations: list[str] = []
    completed: list[str] = []

    def worker() -> None:
        with provider.lease(timeout=20) as h:
            with lock:
                live[h.info.uid] += 1
                if live[h.info.uid] > 1:
                    violations.append(h.info.uid)
            time.sleep(0.01)
            with lock:
                live[h.info.uid] -= 1
                completed.append(h.info.uid)

    errors = run_threads(worker, n=20)
    assert not errors, errors
    assert not violations, f"same instance handed out concurrently: {violations}"
    assert len(completed) == 20


def test_lease_releases_on_exception():
    provider = FakeProvider(n=1)
    with pytest.raises(RuntimeError), provider.lease() as h:
        uid = h.info.uid
        raise RuntimeError("boom")
    with provider.lease() as h2:            # 没泄漏的话立刻就能再拿到
        assert h2.info.uid == uid


def test_exhausted_pool_blocks_then_times_out():
    provider = FakeProvider(n=1)
    with provider.lease(), pytest.raises(TimeoutError):
        provider.lease(timeout=0.3)


def test_non_blocking_raises_busy():
    provider = FakeProvider(n=1)
    with provider.lease(), pytest.raises(Busy):
        provider.lease(block=False)


def test_specific_instance_busy():
    provider = FakeProvider(n=2)
    with provider.lease(instance_id="i0") as h:
        assert h.info.id == "i0"
        with pytest.raises(Busy):
            provider.lease(instance_id="i0", block=False)
        with provider.lease(instance_id="i1") as h2:
            assert h2.info.id == "i1"


def test_lease_key_is_namespaced_by_provider():
    """多个 provider 的 id 会撞（plain 的 "default"），uid 必须带前缀。"""
    store = MemoryLease()
    a, b = FakeProvider(n=1, name="a"), FakeProvider(n=1, name="b")
    pool = Pool([a, b], lease=store)
    with pool.lease(provider="a") as h1, pool.lease(provider="b") as h2:
        assert h1.info.id == h2.info.id == "i0"       # 同 id
        assert h1.lease.key != h2.lease.key            # 但不同 key
        assert len(store.held_keys()) == 2


def test_duplicate_provider_names_rejected():
    with pytest.raises(ValueError, match="unique"):
        Pool([FakeProvider(name="dup"), FakeProvider(name="dup")])


def test_release_is_idempotent():
    provider = FakeProvider(n=1)
    h = provider.lease()
    h.close()
    h.close()
    assert provider.release_calls == ["i0"]


# --------------------------------------------------------------------------- #
# MemoryLease 本身
# --------------------------------------------------------------------------- #


def test_expired_lease_cannot_be_renewed():
    """哪怕还没被别人抢走，过期了也续不回来。

    Redis 后端里 key 到点自动消失，renew 必然失败。内存后端如果只比 token 不看过期
    时间，同一个卡顿场景在两个后端上结果完全相反 —— 一个悄悄续上接着发 CDP 命令，
    一个抛 LeaseLost。两边都走"续不上"这条更安全的路径。
    """
    store = MemoryLease()
    token = store.acquire("k", ttl=0.05)
    assert token is not None
    assert store.renew("k", token, ttl=0.05) is True

    time.sleep(0.08)
    assert store.renew("k", token, ttl=1.0) is False
    assert store.held_keys() == set()


def test_release_will_not_delete_someone_elses_lock():
    """TTL 过期后你的 release 不能把接手者的锁删掉。"""
    store = MemoryLease()
    mine = store.acquire("k", ttl=0.05)
    time.sleep(0.08)
    theirs = store.acquire("k", ttl=5.0)         # 接手者
    assert theirs is not None and theirs != mine

    store.release("k", mine)                     # 迟到的清理
    assert store.held_keys() == {"k"}
    assert store.renew("k", theirs, ttl=5.0) is True


def test_expired_lease_can_be_acquired_by_someone_else():
    store = MemoryLease()
    first = store.acquire("k", ttl=0.05)
    assert store.acquire("k", ttl=5.0) is None   # 还在有效期内
    time.sleep(0.08)
    second = store.acquire("k", ttl=5.0)
    assert second is not None and second != first
