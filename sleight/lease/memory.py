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
        now = time.monotonic()
        with self._lock:
            entry = self._held.get(key)
            if entry is not None and entry[1] > now:
                return None                      # 仍在有效期内，被占
            token = secrets.token_hex(8)
            self._held[key] = (token, now + ttl)
            return token

    def renew(self, key: str, token: str, *, ttl: float) -> bool:
        with self._lock:
            entry = self._held.get(key)
            if entry is None or entry[0] != token:
                return False                     # 已过期并被别人抢走
            self._held[key] = (token, time.monotonic() + ttl)
            return True

    def release(self, key: str, token: str) -> None:
        with self._lock:
            entry = self._held.get(key)
            if entry is not None and entry[0] == token:   # CAS：不能删别人的锁
                del self._held[key]

    # ---- 仅用于测试与诊断 ----

    def held_keys(self) -> set[str]:
        now = time.monotonic()
        with self._lock:
            return {k for k, (_, exp) in self._held.items() if exp > now}
