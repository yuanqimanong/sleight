"""部署层的异常。挂在 :class:`~sleight.core.errors.SleightError` 下，调用方一个
``except SleightError`` 能同时接住驱动层和部署层。"""

from __future__ import annotations

from ..core.errors import SleightError

__all__ = ["CommandFailed", "DeployError", "PreflightFailed"]


class DeployError(SleightError):
    """部署或运维动作失败。"""


class CommandFailed(DeployError):
    """目标机上的命令返回了非 0。

    :ivar result: 完整的 :class:`~sleight.deploy.runner.CommandResult`，含 stdout/stderr
    """

    def __init__(self, message: str, result: object | None = None) -> None:
        super().__init__(message)
        self.result = result


class PreflightFailed(DeployError):
    """体检没过。

    :ivar checks: 全部检查项，含通过的 —— 报错时要能一次看清全貌
    """

    def __init__(self, message: str, checks: list[object] | None = None) -> None:
        super().__init__(message)
        self.checks = checks or []
