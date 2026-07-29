"""进程内租约。

单进程多线程并发够用。**必须由 Pool 长期持有同一个实例** —— 如果每次 ``lease()``
现建一个，各线程持有不同锁表，排他直接失效。
"""

from __future__ import annotations

import secrets
import threading
import time

__all__ = ["MemoryLease"]


class MemoryLease:
    """``key -> (owner_token, expires_at)``。"""

    __slots__ = ("_held", "_lock")

    def __init__(self) -> None:
        self._held: dict[str, tuple[str, float]] = {}
        self._lock = threading.Lock()

    def acquire(self, key: str, *, ttl: float) -> str | None:
        """抢锁。

        :param key: 租约 key，通常是 ``"{namespace}:{provider}:{id}"``
        :param ttl: 存活秒数
        :returns: 抢到了返回 owner_token，被占返回 ``None``
        """
        now = time.monotonic()
        with self._lock:
            entry = self._held.get(key)
            if entry is not None and entry[1] > now:
                return None                      # 仍在有效期内，被占
            token = secrets.token_hex(8)
            self._held[key] = (token, now + ttl)
            return token

    def renew(self, key: str, token: str, *, ttl: float) -> bool:
        """续期。**已经过期的租约续不回来**，哪怕还没被别人抢走。

        Redis 后端里 key 到点就自动消失，`renew` 必然失败；这里如果只比 token 不看
        过期时间，同一个卡顿场景在两个后端上会有完全不同的结果 —— 内存后端悄悄续上
        接着跑，redis 后端抛 LeaseLost。宁可两边都走"续不上"这条更安全的路径。

        :param key: 租约 key
        :param token: 持有者 token
        :param ttl: 新的存活秒数
        :returns: 成功续上返回 True
        """
        now = time.monotonic()
        with self._lock:
            entry = self._held.get(key)
            if entry is None or entry[0] != token or entry[1] <= now:
                return False              # 不存在 / 被别人抢走 / 自己已经过期
            self._held[key] = (token, now + ttl)
            return True

    def release(self, key: str, token: str) -> None:
        """释放。**比对 token 之后才删** —— 否则 TTL 过期后你的 release 会删掉别人的锁。

        :param key: 租约 key
        :param token: 自己的 owner_token。对不上就什么都不做
        """
        with self._lock:
            entry = self._held.get(key)
            if entry is not None and entry[0] == token:   # CAS：不能删别人的锁
                del self._held[key]

    # ---- 仅用于测试与诊断 ----

    def held_keys(self) -> set[str]:
        """当前仍在有效期内的 key。仅用于测试与诊断。"""
        now = time.monotonic()
        with self._lock:
            return {k for k, (_, exp) in self._held.items() if exp > now}
