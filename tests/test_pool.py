"""Pool 的发现、故障隔离与筛选。"""

from __future__ import annotations

import time

import pytest

from sleight import Pool
from sleight.core.errors import ConnectionError, NotFound

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


def test_where_that_matches_nothing_does_not_spin_forever():
    pool = Pool([FakeProvider(2)])
    with pytest.raises(Exception) as exc:
        pool.lease(where=lambda i: False, timeout=0.3)
    assert "no free instance" in str(exc.value) or "within" in str(exc.value)


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
