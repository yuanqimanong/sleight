"""跨进程租约，用 Redis 存锁表。

**什么时候需要它**：触发条件是**有几个进程**，不是有几个实例、也不是有几个 Manager。
单进程哪怕开二十个线程、跨三个 Manager 管三十个实例，:class:`~sleight.lease.memory.MemoryLease`
就够了。只有多个进程或多台机器共享同一批浏览器实例时，锁表才必须搬出进程。

**它依然是协作式排他，不是强制隔离。** Redis 里的 token 不是 fencing token ——
真正的 fencing 要求受保护资源在每次操作时拒绝旧代次，而 Chrome 不认识 Redis 里的
任何东西。缓解手段是短 TTL + 续租，续租一失败就立刻掐断 WebSocket（见
:class:`~sleight.pool.InstanceHandle`），把窗口从"一个 TTL"压到"一次续租周期"。
窗口无法归零，这是协作式租约的固有性质。
"""

from __future__ import annotations

import secrets
from typing import Any

__all__ = ["RedisLease"]


try:
    import redis as _redis
except ModuleNotFoundError as exc:      # pragma: no cover - 取决于装没装 extra
    raise ModuleNotFoundError(
        "RedisLease needs the redis client, which is an optional dependency:\n"
        '    pip install "sleight[redis]"\n'
        "Single-process setups (even with many threads) do not need it — "
        "the default MemoryLease already provides exclusion."
    ) from exc


#: 释放：**先比对 token 再删**。
#:
#: 少了这个比对，你的 ``release`` 会在 TTL 过期之后删掉**接手者**的锁 —— 恰好是
#: 租约要防的那件事。必须在 Redis 侧原子完成，客户端先 GET 再 DEL 中间有窗口。
_RELEASE_LUA = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
end
return 0
"""

#: 续期：同样先比对 token。
#:
#: key 到点就被 Redis 自己删了，所以"已经过期的租约"在这里必然续不上 ——
#: ``get`` 返回 nil，比对失败，返回 0。MemoryLease 特意实现了同样的语义。
_RENEW_LUA = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('pexpire', KEYS[1], ARGV[2])
end
return 0
"""


class RedisLease:
    """把租约存进 Redis，供多进程 / 多机共享。

    满足 :class:`~sleight.lease.base.Lease` 协议，直接传给
    :class:`~sleight.pool.Pool`::

        from sleight import Pool
        from sleight.lease import RedisLease

        pool = Pool(providers, lease=RedisLease("redis://127.0.0.1:6379/0"),
                    namespace="prod")

    :param url: Redis 连接串，如 ``redis://:pw@host:6379/0``。与 ``client`` 二选一
    :param client: 已有的 redis 客户端（连接池共用时给这个）。与 ``url`` 二选一
    :param prefix: Redis key 前缀，用来和同一个库里的其它数据分开。
        这**不是** :class:`~sleight.pool.Pool` 的 ``namespace`` —— 后者区分环境
        （prod / staging），最终 key 形如 ``sleight:prod:cloakbrowser:5edcc28a``
    :raises ValueError: ``url`` 和 ``client`` 都给了或都没给
    """

    __slots__ = ("_client", "_prefix", "_release", "_renew")

    def __init__(
        self,
        url: str | None = None,
        *,
        client: Any = None,
        prefix: str = "sleight",
    ) -> None:
        if (url is None) == (client is None):
            raise ValueError("RedisLease takes exactly one of url= or client=")

        self._client = client if client is not None else _redis.Redis.from_url(url)
        self._prefix = prefix.rstrip(":")
        # 注册成 Script 对象，redis-py 会走 EVALSHA 并在 NOSCRIPT 时自动回落到 EVAL
        self._release = self._client.register_script(_RELEASE_LUA)
        self._renew = self._client.register_script(_RENEW_LUA)

    def __repr__(self) -> str:
        return f"<RedisLease prefix={self._prefix!r}>"

    def _key(self, key: str) -> str:
        return f"{self._prefix}:{key}"

    # ------------------------------------------------------------------ #
    # Lease 协议
    # ------------------------------------------------------------------ #

    def acquire(self, key: str, *, ttl: float) -> str | None:
        """抢锁。``SET NX PX`` 一条命令，天然原子。

        :param key: 租约 key，由 Pool 拼成 ``"{namespace}:{provider}:{id}"``
        :param ttl: 存活秒数。到点 Redis 自己删，持有者崩溃也不会留下死锁
        :returns: 抢到了返回 owner_token；被别人占着返回 ``None``
        """
        token = secrets.token_hex(16)
        acquired = self._client.set(self._key(key), token, nx=True, px=_ms(ttl))
        return token if acquired else None

    def renew(self, key: str, token: str, *, ttl: float) -> bool:
        """续期。**只有仍然是记录在案的持有者才能续。**

        已经过期的租约续不回来 —— key 早被 Redis 删了，比对必然失败。这一点和
        :class:`~sleight.lease.memory.MemoryLease` 保持一致，免得同一段代码在两个
        后端上行为相反。

        :param key: 租约 key
        :param token: :meth:`acquire` 拿到的 owner_token
        :param ttl: 新的存活秒数
        :returns: 续上了返回 True；已过期或已被别人抢走返回 False
        """
        return bool(self._renew(keys=[self._key(key)], args=[token, _ms(ttl)]))

    def release(self, key: str, token: str) -> None:
        """释放。**比对 token 之后才删**，绝不动别人的锁。

        对已经过期（或本就不存在）的 key 是静默 no-op —— 释放要幂等，收尾路径上
        再抛异常只会盖住调用方原本的错误。

        :param key: 租约 key
        :param token: 自己的 owner_token。对不上就什么都不做
        """
        self._release(keys=[self._key(key)], args=[token])

    # ------------------------------------------------------------------ #
    # 诊断
    # ------------------------------------------------------------------ #

    def held_keys(self) -> set[str]:
        """当前被持有的租约 key（已去掉前缀）。仅用于运维排查。

        走 ``SCAN`` 而不是 ``KEYS``：后者在大库上会阻塞整个 Redis。
        """
        cut = len(self._prefix) + 1
        return {
            (k.decode() if isinstance(k, bytes) else k)[cut:]
            for k in self._client.scan_iter(match=f"{self._prefix}:*")
        }


def _ms(seconds: float) -> int:
    """秒转毫秒。至少 1 ms —— ``PX 0`` 会被 Redis 当成非法参数拒掉。"""
    return max(int(seconds * 1000), 1)
