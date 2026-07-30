"""跨进程租约（Redis 后端）。

默认跑在一个假 Redis 上 —— 它实现了 ``SET NX PX`` / ``GET`` / ``DEL`` / ``PEXPIRE``
和一个**可控时钟**，因此过期语义可以被确定性地测出来，不用 sleep 也不用真服务器。

设了 ``SLEIGHT_TEST_REDIS_URL`` 就会额外跑一组打真服务器的用例；没设则 skip。
Lua 脚本的实际行为只有真服务器能验证，假 Redis 只保证调用语义对得上。
"""

from __future__ import annotations

import os
import threading

import pytest

from sleight import Pool
from sleight.lease import RedisLease
from sleight.lease.base import Lease

from .conftest import FakeProvider, run_threads

# --------------------------------------------------------------------------- #
# 假 Redis
# --------------------------------------------------------------------------- #


class FakeRedis:
    """够 RedisLease 用的最小 Redis 替身，带可控时钟。

    ``clock`` 是毫秒。测试自己推进它，于是"租约过期"这件事既确定又不耗时。
    """

    def __init__(self) -> None:
        self.data: dict[str, tuple[str, float]] = {}     # key -> (value, 过期时刻 ms)
        self.clock = 0.0
        self.evalsha_calls = 0

    # —— 时钟 ——

    def advance(self, ms: float) -> None:
        self.clock += ms

    def _live(self, key: str) -> tuple[str, float] | None:
        entry = self.data.get(key)
        if entry is None:
            return None
        if entry[1] <= self.clock:            # 到点，Redis 会当它不存在
            del self.data[key]
            return None
        return entry

    # —— Redis 命令 ——

    def set(self, key, value, *, nx=False, px=None):
        if nx and self._live(key) is not None:
            return None
        self.data[key] = (value, self.clock + (px if px is not None else 1e12))
        return True

    def get(self, key):
        entry = self._live(key)
        return entry[0] if entry else None

    def delete(self, key):
        return 1 if self.data.pop(key, None) is not None else 0

    def pexpire(self, key, ms):
        entry = self._live(key)
        if entry is None:
            return 0
        self.data[key] = (entry[0], self.clock + ms)
        return 1

    def scan_iter(self, match=None):
        prefix = (match or "*").rstrip("*")
        return [k for k in list(self.data) if self._live(k) and k.startswith(prefix)]

    # —— Lua ——

    def register_script(self, source: str):
        """按脚本内容返回等价的 Python 实现，保持 CAS 语义。"""
        deletes = "del" in source

        def run(keys, args):
            self.evalsha_calls += 1
            key = keys[0]
            if self.get(key) != args[0]:          # 比对 token —— 这就是 CAS
                return 0
            return self.delete(key) if deletes else self.pexpire(key, int(args[1]))

        return run


@pytest.fixture
def fake() -> FakeRedis:
    return FakeRedis()


@pytest.fixture
def store(fake: FakeRedis) -> RedisLease:
    return RedisLease(client=fake, prefix="sleight")


# --------------------------------------------------------------------------- #
# 构造
# --------------------------------------------------------------------------- #


def test_it_satisfies_the_lease_protocol(store: RedisLease):
    """Pool 只认协议，不认具体类型。"""
    assert isinstance(store, Lease)


def test_url_and_client_are_mutually_exclusive(fake: FakeRedis):
    with pytest.raises(ValueError, match="exactly one"):
        RedisLease()
    with pytest.raises(ValueError, match="exactly one"):
        RedisLease("redis://localhost", client=fake)


def test_keys_are_prefixed(fake: FakeRedis):
    """前缀把 sleight 的锁和同一个库里的其它数据分开。"""
    RedisLease(client=fake, prefix="myapp").acquire("prod:cb:p1", ttl=30)
    assert list(fake.data) == ["myapp:prod:cb:p1"]


def test_a_trailing_colon_in_the_prefix_does_not_double_up(fake: FakeRedis):
    RedisLease(client=fake, prefix="myapp:").acquire("k", ttl=30)
    assert list(fake.data) == ["myapp:k"]


# --------------------------------------------------------------------------- #
# acquire / renew / release
# --------------------------------------------------------------------------- #


def test_acquire_returns_a_token_and_locks_out_others(store: RedisLease):
    token = store.acquire("cb:p1", ttl=30)
    assert token and len(token) >= 16
    assert store.acquire("cb:p1", ttl=30) is None


def test_two_keys_do_not_interfere(store: RedisLease):
    assert store.acquire("cb:p1", ttl=30)
    assert store.acquire("cb:p2", ttl=30)


def test_tokens_are_unique_per_acquisition(store: RedisLease):
    first = store.acquire("k", ttl=30)
    store.release("k", first)
    assert store.acquire("k", ttl=30) != first


def test_renew_extends_the_deadline(store: RedisLease, fake: FakeRedis):
    token = store.acquire("k", ttl=1.0)
    fake.advance(800)
    assert store.renew("k", token, ttl=1.0) is True
    fake.advance(800)                       # 没续的话这里已经过期了
    assert store.acquire("k", ttl=1.0) is None


def test_an_expired_lease_cannot_be_renewed(store: RedisLease, fake: FakeRedis):
    """key 到点就被 Redis 自己删了，比对必然失败。

    :class:`~sleight.lease.memory.MemoryLease` 特意实现了同样的语义 —— 否则同一段
    "进程卡顿了一下"的代码在两个后端上结果相反：一个悄悄续上继续发 CDP 命令，
    一个抛 LeaseLost。
    """
    token = store.acquire("k", ttl=1.0)
    fake.advance(1500)
    assert store.renew("k", token, ttl=30) is False


def test_renew_with_someone_elses_token_fails(store: RedisLease):
    store.acquire("k", ttl=30)
    assert store.renew("k", "not-my-token", ttl=30) is False


def test_release_only_deletes_your_own_lock(store: RedisLease, fake: FakeRedis):
    """TTL 过期之后你的 release 绝不能删掉接手者的锁 —— 这正是租约要防的事。"""
    mine = store.acquire("k", ttl=1.0)
    fake.advance(1500)
    theirs = store.acquire("k", ttl=30)        # 接手者
    assert theirs and theirs != mine

    store.release("k", mine)                   # 迟到的清理
    assert fake.get("sleight:k") == theirs
    assert store.renew("k", theirs, ttl=30) is True


def test_release_of_a_missing_key_is_silent(store: RedisLease):
    """释放要幂等 —— 收尾路径上再抛异常只会盖住调用方原本的错误。"""
    store.release("never-held", "whatever")
    token = store.acquire("k", ttl=30)
    store.release("k", token)
    store.release("k", token)


def test_release_frees_the_key_for_someone_else(store: RedisLease):
    token = store.acquire("k", ttl=30)
    store.release("k", token)
    assert store.acquire("k", ttl=30) is not None


def test_cas_goes_through_lua_not_two_round_trips(store: RedisLease, fake: FakeRedis):
    """比对和删除必须在 Redis 侧原子完成。

    客户端先 GET 再 DEL 中间有窗口：GET 通过之后锁可能刚好过期并被别人抢走，
    然后你把**他的**锁删了。
    """
    token = store.acquire("k", ttl=30)
    store.renew("k", token, ttl=30)
    store.release("k", token)
    assert fake.evalsha_calls == 2, "renew 和 release 都必须走脚本"


def test_a_sub_millisecond_ttl_is_not_rejected(store: RedisLease, fake: FakeRedis):
    """``PX 0`` 会被真 Redis 当成非法参数拒掉。"""
    assert store.acquire("k", ttl=0.0001) is not None
    assert fake.data["sleight:k"][1] - fake.clock >= 1


# --------------------------------------------------------------------------- #
# 诊断
# --------------------------------------------------------------------------- #


def test_held_keys_strips_the_prefix(store: RedisLease):
    store.acquire("prod:cb:p1", ttl=30)
    store.acquire("prod:cb:p2", ttl=30)
    assert store.held_keys() == {"prod:cb:p1", "prod:cb:p2"}


def test_held_keys_ignores_expired_entries(store: RedisLease, fake: FakeRedis):
    store.acquire("short", ttl=1.0)
    store.acquire("long", ttl=60.0)
    fake.advance(1500)
    assert store.held_keys() == {"long"}


def test_repr_does_not_leak_the_connection_string(fake: FakeRedis):
    """Redis URL 里可能带密码，而 repr 会进日志和 traceback。"""
    store = RedisLease(client=fake, prefix="p")
    assert "password" not in repr(store) and "://" not in repr(store)


# --------------------------------------------------------------------------- #
# 接进 Pool
# --------------------------------------------------------------------------- #


def test_pool_accepts_it_and_namespaces_the_key(fake: FakeRedis):
    """Pool 的 namespace 区分环境，RedisLease 的 prefix 区分"这是 sleight 的数据"。"""
    pool = Pool([FakeProvider(1)], lease=RedisLease(client=fake), namespace="prod")
    with pool.lease() as handle:
        assert handle.info.uid == "fake:i0"
        assert list(fake.data) == ["sleight:prod:fake:i0"]
    assert fake.data == {}, "退出 with 之后必须把锁还掉"


def test_exclusive_across_threads_on_a_shared_store(fake: FakeRedis):
    """20 线程抢 3 个实例，共用同一份 Redis 锁表 —— 任一时刻同一 uid 至多一个持有者。

    这是 MemoryLease 那条不变量的跨后端版本。
    """
    provider = FakeProvider(3)
    pool = Pool([provider], lease=RedisLease(client=fake))
    live: dict[str, int] = {}
    lock = threading.Lock()
    violations: list[str] = []
    done: list[str] = []

    def worker() -> None:
        with pool.lease(timeout=20) as handle:
            uid = handle.info.uid
            with lock:
                live[uid] = live.get(uid, 0) + 1
                if live[uid] > 1:
                    violations.append(uid)
            with lock:
                live[uid] -= 1
                done.append(uid)

    errors = run_threads(worker, n=20)
    assert not errors, errors
    assert not violations, f"same instance handed out concurrently: {violations}"
    assert len(done) == 20


def test_two_pools_sharing_one_store_do_not_double_book(fake: FakeRedis):
    """这才是 RedisLease 存在的理由 —— 模拟两个进程各有自己的 Pool。"""
    store = RedisLease(client=fake)
    a = Pool([FakeProvider(1, name="p")], lease=store, namespace="prod")
    b = Pool([FakeProvider(1, name="p")], lease=store, namespace="prod")

    from sleight.core.errors import Busy

    with a.lease() as held:
        assert held.info.uid == "p:i0"
        with pytest.raises(Busy):
            b.lease(block=False)

    with b.lease(block=False):              # 前一个释放之后 b 立刻能拿到
        pass


def test_separate_namespaces_do_not_collide(fake: FakeRedis):
    store = RedisLease(client=fake)
    prod = Pool([FakeProvider(1, name="p")], lease=store, namespace="prod")
    staging = Pool([FakeProvider(1, name="p")], lease=store, namespace="staging")
    with prod.lease(), staging.lease():     # 同一个 uid，不同环境，互不阻塞
        assert set(fake.data) == {"sleight:prod:p:i0", "sleight:staging:p:i0"}


# --------------------------------------------------------------------------- #
# 真 Lua：fakeredis[lua] 带 Lua 解释器，CAS 脚本在这里是被真正执行的
#
# 上面那个手写替身只保证"调用语义对得上"，它把 Lua 换成了等价的 Python。真正的
# 脚本有没有语法错、KEYS/ARGV 下标对不对、返回值能不能被 bool() 正确解读，只有真
# 解释器能验。
# --------------------------------------------------------------------------- #

fakeredis = pytest.importorskip("fakeredis", reason="fakeredis[lua] runs the real Lua")


@pytest.fixture
def lua_store() -> RedisLease:
    return RedisLease(client=fakeredis.FakeStrictRedis(), prefix="sleight")


def test_lua_release_is_a_real_compare_and_delete(lua_store: RedisLease):
    token = lua_store.acquire("k", ttl=30)
    assert token

    lua_store.release("k", "not-my-token")
    assert lua_store.held_keys() == {"k"}, "别人的 token 不能删掉这把锁"

    lua_store.release("k", token)
    assert lua_store.held_keys() == set()


def test_lua_renew_is_a_real_compare_and_expire(lua_store: RedisLease):
    token = lua_store.acquire("k", ttl=30)
    assert lua_store.renew("k", token, ttl=60) is True
    assert lua_store.renew("k", "not-my-token", ttl=60) is False


def test_lua_scripts_survive_a_real_ttl_expiry(lua_store: RedisLease):
    """真实过期：key 被 Redis 自己删掉，脚本里的 get 拿到 nil。"""
    import time

    token = lua_store.acquire("k", ttl=0.15)
    assert token
    time.sleep(0.35)
    assert lua_store.renew("k", token, ttl=30) is False, "过期的租约续不回来"
    assert lua_store.acquire("k", ttl=30) is not None, "过期后别人应该能抢到"


def test_full_lease_cycle_through_a_pool_on_real_lua(lua_store: RedisLease):
    """两个 Pool 共用一份锁表 —— 模拟两个进程，这才是 RedisLease 存在的理由。"""
    from sleight.core.errors import Busy

    a = Pool([FakeProvider(1, name="p")], lease=lua_store, namespace="prod")
    b = Pool([FakeProvider(1, name="p")], lease=lua_store, namespace="prod")

    with a.lease() as held:
        assert lua_store.held_keys() == {"prod:p:i0"}
        with pytest.raises(Busy):
            b.lease(block=False)
        assert held.info.uid == "p:i0"

    assert lua_store.held_keys() == set(), "退出 with 之后锁必须还掉"
    with b.lease(block=False):
        pass


# --------------------------------------------------------------------------- #
# 打真 Redis（设了 SLEIGHT_TEST_REDIS_URL 才跑）
# --------------------------------------------------------------------------- #

REAL_URL = os.environ.get("SLEIGHT_TEST_REDIS_URL")
real_redis = pytest.mark.skipif(
    not REAL_URL, reason="set SLEIGHT_TEST_REDIS_URL to run against a real server"
)


@pytest.fixture
def live_store() -> RedisLease:
    store = RedisLease(REAL_URL, prefix=f"sleight-test-{os.getpid()}")
    yield store
    for key in store.held_keys():
        store._client.delete(store._key(key))


@real_redis
@pytest.mark.redis
def test_real_server_round_trip(live_store: RedisLease):
    """Lua 脚本的实际行为只有真服务器能验证。"""
    token = live_store.acquire("k", ttl=30)
    assert token
    assert live_store.acquire("k", ttl=30) is None
    assert live_store.renew("k", token, ttl=30) is True
    assert live_store.renew("k", "wrong", ttl=30) is False

    live_store.release("k", "wrong")
    assert live_store.held_keys() == {"k"}, "CAS：不能删掉别人的锁"

    live_store.release("k", token)
    assert live_store.held_keys() == set()


@real_redis
@pytest.mark.redis
def test_real_server_expires_the_key(live_store: RedisLease):
    import time

    token = live_store.acquire("k", ttl=0.2)
    assert token
    time.sleep(0.4)
    assert live_store.renew("k", token, ttl=30) is False, "过期的租约续不回来"
    assert live_store.acquire("k", ttl=30) is not None, "过期后别人应该能抢到"
