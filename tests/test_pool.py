"""Pool 的发现、故障隔离与筛选。"""

from __future__ import annotations

import time

import pytest

from sleight import Pool
from sleight import pool as pool_module
from sleight.core.errors import ConnectionError, NotFound, TimeoutError
from sleight.core.types import InstanceInfo

from .conftest import FakeProvider


def test_discovery_aggregates_all_providers():
    pool = Pool([FakeProvider(2, name="hk"), FakeProvider(3, name="sg")])
    uids = {i.uid for i in pool.discover()}
    assert uids == {"hk:i0", "hk:i1", "sg:i0", "sg:i1", "sg:i2"}


def test_discovery_is_cached():
    p = FakeProvider(1)
    pool = Pool([p], discovery_ttl=60)
    pool.discover()
    pool.discover()
    assert p.list_calls == 1
    pool.discover(force=True)
    assert p.list_calls == 2


def test_the_first_discovery_is_never_served_from_the_empty_cache(monkeypatch):
    """``time.monotonic()`` 在 Linux 上是**开机以来**的秒数。

    刚起来的容器 / CI runner 上它可能只有 12 —— 而 ``_cache_at`` 的初值若是 ``0.0``，
    ``now - 0.0 < discovery_ttl`` 就成立，第一次 ``discover()`` 直接返回空缓存，
    provider 一次都不会被问到。表现是进程启动后的头一分钟内 ``lease()`` 找不到任何
    实例，之后又莫名其妙好了。

    开发机开机久了永远碰不到这条，CI 上必现 —— 这条测试就是为了不再靠运气。
    """
    monkeypatch.setattr(pool_module.time, "monotonic", lambda: 12.5)
    p = FakeProvider(3)
    pool = Pool([p], discovery_ttl=60)

    assert len(pool.discover()) == 3
    assert p.list_calls == 1, "第一次 discover() 必须真的问一遍 provider"

    pool.discover()                     # 时间没走，这次才该命中缓存
    assert p.list_calls == 1


def test_a_broken_provider_does_not_block_the_pool():
    """单个 Manager 挂掉必须只是少几个实例，不是整池不可用。"""
    good = FakeProvider(2, name="good")
    bad = FakeProvider(2, name="bad", fail=ConnectionError("tunnel down"))
    pool = Pool([good, bad], discovery_ttl=0)

    uids = {i.uid for i in pool.discover()}
    assert uids == {"good:i0", "good:i1"}

    with pool.lease() as h:               # 仍然能租到
        assert h.info.provider == "good"


def test_failing_provider_enters_backoff():
    bad = FakeProvider(1, name="bad", fail=ConnectionError("down"))
    pool = Pool([bad], discovery_ttl=0)

    pool.discover()
    assert bad.list_calls == 1
    pool.discover()                        # 在 backoff 里，不该再打它
    assert bad.list_calls == 1
    assert pool._failures["bad"][0] == 1


def test_backoff_recovers_after_the_window():
    bad = FakeProvider(1, name="bad", fail=ConnectionError("down"))
    pool = Pool([bad], discovery_ttl=0)
    pool.discover()
    pool._failures["bad"] = (1, time.monotonic() - 1)     # 假装窗口已过
    bad.fail = None
    assert len(pool.discover()) == 1
    assert "bad" not in pool._failures


def test_where_predicate_filters_on_tags():
    p = FakeProvider(3, tags={"i0": {"us"}, "i1": {"hk"}, "i2": {"us", "prod"}})
    pool = Pool([p])
    with pool.lease(where=lambda i: "us" in i.tags) as h:
        assert h.info.id in {"i0", "i2"}


def test_where_that_matches_nothing_fails_immediately():
    """「都被占着」和「根本没有这个实例」是两回事，报错也必须是两个。"""
    pool = Pool([FakeProvider(2)])
    started = time.monotonic()
    with pytest.raises(NotFound) as exc:
        pool.lease(where=lambda i: False, timeout=30)
    assert time.monotonic() - started < 1.0, "blocked to the timeout on a filter that can't match"
    assert "fake:i0" in str(exc.value), "the error must list what IS visible — it's a typo 9/10 times"


def test_lease_by_name():
    p = FakeProvider(3)
    with Pool([p]).lease(name="fake-1") as h:
        assert h.info.name == "fake-1"


def test_lease_by_one_of_several_names():
    with Pool([FakeProvider(3)]).lease(names=["fake-2", "nope"]) as h:
        assert h.info.name == "fake-2"


def test_a_misspelled_name_says_so_instead_of_timing_out():
    """打错名字却阻塞到 TimeoutError —— 从错误信息完全看不出是配错了。"""
    with pytest.raises(NotFound) as exc:
        Pool([FakeProvider(2)]).lease(name="Win-US-02", timeout=30)
    text = str(exc.value)
    assert "'Win-US-02'" in text
    assert "fake-0" in text and "fake-1" in text


def test_name_and_names_together_is_a_mistake():
    with pytest.raises(ValueError, match="not both"):
        Pool([FakeProvider(1)]).lease(name="a", names=["b"])


def test_an_empty_pool_says_it_is_empty():
    with pytest.raises(NotFound, match="pool is empty"):
        Pool([FakeProvider(0)]).lease(name="anything")


def test_long_instance_lists_are_truncated_in_the_error():
    with pytest.raises(NotFound) as exc:
        Pool([FakeProvider(40)]).lease(name="nope")
    assert "more" in str(exc.value)
    assert len(str(exc.value)) < 600, "unreadable wall of names"


def test_ready_only_keeps_blocking_because_it_filters_state_not_identity():
    """实例这会儿没起来不代表它不存在 —— 等下去是有意义的，报 NotFound 不是。"""
    p = FakeProvider(2)
    p.list_instances = lambda: [                      # type: ignore[method-assign]
        InstanceInfo(id="i0", provider="fake", ready=False, name="fake-0"),
    ]
    with pytest.raises(TimeoutError):
        Pool([p]).lease(ready_only=True, timeout=0.3)


# --------------------------------------------------------------------------- #
# lease_many
# --------------------------------------------------------------------------- #


def test_lease_many_hands_out_distinct_instances():
    handles = Pool([FakeProvider(3)]).lease_many(3)
    try:
        assert len({h.info.uid for h in handles}) == 3
    finally:
        for h in handles:
            h.close()


def test_lease_many_rolls_back_everything_when_one_fails():
    """拿到 3 个第 4 个超时，那 3 个必须还回去 —— 否则它们被占满一整个 TTL。"""
    pool = Pool([FakeProvider(3)])
    with pytest.raises(TimeoutError):
        pool.lease_many(4, timeout=0.3)

    # 全还回去了才租得到下一批
    handles = pool.lease_many(3, block=False)
    for h in handles:
        h.close()


def test_lease_many_timeout_covers_the_whole_batch():
    """每个都给 timeout 的话，一批 8 个最坏要等 8 倍 —— 而调用方以为写的是 timeout。"""
    pool = Pool([FakeProvider(1)])
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        pool.lease_many(4, timeout=0.6)
    assert time.monotonic() - started < 2.0


def test_lease_many_refuses_nonsense():
    pool = Pool([FakeProvider(2)])
    with pytest.raises(ValueError, match="n >= 1"):
        pool.lease_many(0)
    with pytest.raises(ValueError, match="instance_id"):
        pool.lease_many(2, instance_id="i0")


def test_unknown_instance_id_is_not_found():
    pool = Pool([FakeProvider(2)])
    with pytest.raises(NotFound):
        pool.lease(instance_id="nope", block=False)


def test_ensure_ready_is_called_on_acquire():
    p = FakeProvider(1)
    with p.lease() as h:
        assert p.ready_calls == [h.info.id]


def test_instances_are_spread_across_providers():
    """否则 first-free 会一直把第一个 Manager 塞满。"""
    pool = Pool([FakeProvider(2, name="a"), FakeProvider(2, name="b")], discovery_ttl=60)
    seen = set()
    handles = [pool.lease() for _ in range(4)]
    try:
        seen = {h.info.provider for h in handles}
    finally:
        for h in handles:
            h.close()
    assert seen == {"a", "b"}
