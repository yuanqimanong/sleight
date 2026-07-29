"""异常体系。所有 sleight 抛出的异常都继承 :class:`SleightError`。"""

from __future__ import annotations

__all__ = [
    "AuthError",
    "Busy",
    "ConnectionError",
    "Crashed",
    "ElementError",
    "InstanceError",
    "LeaseLost",
    "LeaseStillHeld",
    "NotFound",
    "NotReady",
    "ProtocolError",
    "SessionLost",
    "SleightError",
    "TimeoutError",
]


class SleightError(Exception):
    """所有 sleight 异常的根。"""


class ConnectionError(SleightError):
    """端点不可达、隧道断、WebSocket 掉线。"""


class AuthError(SleightError):
    """401 / 403。重试没有意义。"""


class ProtocolError(SleightError):
    """CDP 返回了 error 对象，或消息不符合协议。"""

    def __init__(self, message: str, *, code: int | None = None, method: str | None = None):
        super().__init__(message)
        self.code = code
        self.method = method


class InstanceError(SleightError):
    """实例生命周期相关。"""


class NotFound(InstanceError):
    """instance_id 不存在。

    **只由** ``Provider.status()`` 判定。``launch`` / ``stop`` 的状态码分不清
    "已停止"和"不存在"，照状态码判会把打错的 id 静默当成幂等成功。
    """


class NotReady(InstanceError):
    """实例存在但未启动。"""


class Busy(InstanceError):
    """租约被占。仅用于明确指定实例的场景。"""


class Crashed(InstanceError):
    """实例 running 但 CDP 不通。触发 recover()。"""


class SessionLost(SleightError):
    """连接已恢复但会话状态丢失。

    sleight 不会自动重放业务操作（click/type/open）—— 它不知道哪个是安全的，
    调用方知道。
    """


class LeaseLost(SleightError):
    """续租失败，本 handle 已失效，WebSocket 已被关闭。

    与 :class:`ConnectionError` 区分开是有意的：这个错误**绝不能重试**
    （浏览器已经不归你了），而隧道抖动可以。
    """


class LeaseStillHeld(SleightError):
    """释放租约时仍有活动 Session。

    只有 ``Pool(strict_close=True)`` 才抛；默认是强制关掉它们并记一条 warning。
    Session 已经被关闭、租约也已释放，抛这个异常纯粹是为了让你知道有代码路径漏了
    ``close()``。
    """


class TimeoutError(SleightError):
    """等待条件超时。"""

    def __init__(self, message: str, *, last_value: object = None):
        super().__init__(message)
        self.last_value = last_value


class ElementError(SleightError):
    """选择器没命中 / 元素不可交互 / 被其它元素遮挡。"""
