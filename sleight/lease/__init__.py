"""租约后端。

**协作式排他，不是强制隔离。** 租约只能约束通过 sleight 访问的客户端，拦不住 VNC
上手操作的人、不走 sleight 自己连 CDP 的程序、以及 Manager 自带的 Web UI。
它防的是**你自己的多个任务互相踩**，不是入侵防护。
"""

from .base import Lease, LeaseHandle
from .memory import MemoryLease

__all__ = ["Lease", "LeaseHandle", "MemoryLease"]
