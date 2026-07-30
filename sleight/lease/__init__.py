"""租约后端。

**协作式排他，不是强制隔离。** 租约只能约束通过 sleight 访问的客户端，拦不住 VNC
上手操作的人、不走 sleight 自己连 CDP 的程序、以及 Manager 自带的 Web UI。
它防的是**你自己的多个任务互相踩**，不是入侵防护。
"""

from typing import TYPE_CHECKING, Any

from .base import Lease, LeaseHandle
from .memory import MemoryLease

if TYPE_CHECKING:                       # 让类型检查器看得见，运行时不强制装 redis
    from .redis import RedisLease

__all__ = ["Lease", "LeaseHandle", "MemoryLease", "RedisLease"]


def __getattr__(name: str) -> Any:
    """``RedisLease`` 懒加载。

    它依赖可选的 redis 客户端，急切 import 会让**没装 extra 的用户**连
    ``from sleight.lease import MemoryLease`` 都跑不起来。PEP 562 的模块级
    ``__getattr__`` 让两边都成立：装了就能直接从这里拿，没装则在真正取用时
    抛出一句能照做的提示。
    """
    if name == "RedisLease":
        from .redis import RedisLease

        return RedisLease
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
