"""租约协议。

**这是协作式排他，不是强制隔离。** 它只能约束通过 sleight 访问的客户端，拦不住
VNC 上手动操作的人、不走 sleight 自己连 CDP 的程序、以及 Manager 自己的 Web UI。
租约防的是你自己的多个任务互相踩，不是入侵防护。

token 叫 ``owner_token`` 而不是 fencing token：真正的 fencing 要求 token 单调递增
**且受保护资源在每次操作时拒绝旧代次**。Chrome 不认识 redis token，所以"发命令前
检查"永远存在 TOCTOU。
"""

from __future__ import annotations

import logging
import threading
from typing import Protocol, runtime_checkable

log = logging.getLogger("sleight.lease")

__all__ = ["Lease", "LeaseHandle"]


@runtime_checkable
class Lease(Protocol):
    def acquire(self, key: str, *, ttl: float) -> str | None:
        """成功返回 owner_token，被占返回 None。"""

    def renew(self, key: str, token: str, *, ttl: float) -> bool:
        """续期。token 不匹配（已被别人抢走）返回 False。"""

    def release(self, key: str, token: str) -> None:
        """释放。必须校验 token —— 否则会删掉别人的锁。"""


class LeaseHandle:
    """一次持有。线程安全（``release`` 可能被 handle 关闭路径和续租线程同时调）。"""

    __slots__ = ("_lock", "_lost", "_released", "_store", "key", "token", "ttl")

    def __init__(self, store: Lease, key: str, token: str, ttl: float) -> None:
        self._store = store
        self.key = key
        self.token = token
        self.ttl = ttl
        self._released = False
        self._lost = False
        self._lock = threading.Lock()

    def __repr__(self) -> str:
        state = "released" if self._released else ("lost" if self._lost else "held")
        return f"<LeaseHandle {self.key} {state}>"

    @property
    def lost(self) -> bool:
        """续租失败过。持有者应当立刻停止发命令并关闭连接。"""
        return self._lost

    @property
    def active(self) -> bool:
        return not self._released and not self._lost

    def renew(self) -> bool:
        with self._lock:
            if self._released or self._lost:
                return False
            ok = self._store.renew(self.key, self.token, ttl=self.ttl)
            if not ok:
                self._lost = True
                log.warning("lease %s lost: renewal rejected", self.key)
            return ok

    def release(self) -> None:
        """幂等。"""
        with self._lock:
            if self._released:
                return
            self._released = True
        try:
            self._store.release(self.key, self.token)
        except Exception:
            log.warning("failed to release lease %s", self.key, exc_info=True)
