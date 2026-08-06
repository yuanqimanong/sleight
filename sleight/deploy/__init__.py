"""CloakBrowser Manager 的部署与运维。

**这个子包不会被 ``sleight/__init__.py`` import。** ``import sleight`` 的依赖和开销
和以前完全一样 —— 驱动层不该为了部署功能变重。

引擎只用标准库：SSH 走系统 ``ssh`` 二进制、HTTP 走 :mod:`sleight.core._http`、
配置走 ``tomllib``。只有 :mod:`sleight.deploy.api` 里的 Web 界面需要
``pip install "sleight[ui]"``。

    >>> from sleight.deploy import DeploySpec, Deployer, LocalRunner
    >>> spec = DeploySpec(dir="/srv/cloakbrowser-manager")
    >>> Deployer(spec, LocalRunner()).apply()          # doctest: +SKIP

主机和"这台机上有哪几个 Manager"存在本地 SQLite 里（:class:`Store`）。
"""

from __future__ import annotations

from .engine import Deployer, DeployError, DeployResult, Plan
from .preflight import Check, CheckLevel, preflight
from .runner import CommandResult, LocalRunner, Runner, SSHRunner
from .spec import DEFAULT_IMAGE, DeploySpec
from .store import Deployment, Host, Store

__all__ = [  # noqa: RUF022 - 按语义分组，不按字母序
    "DeploySpec",
    "DEFAULT_IMAGE",
    "Deployer",
    "DeployResult",
    "DeployError",
    "Plan",
    "Runner",
    "LocalRunner",
    "SSHRunner",
    "CommandResult",
    "preflight",
    "Check",
    "CheckLevel",
    "Store",
    "Host",
    "Deployment",
]
