"""``~/.sleight/`` 的位置，以及**旧版 ``hosts.toml`` 的只读导入**。

0.2.x 用 ``hosts.toml`` 存主机清单。从 0.3 起改成 SQLite（见 :mod:`sleight.deploy.store`）
—— 一台机器可以跑多个 Manager，还要记部署流水，TOML 那种"一主机一份配置"的形状撑不住。

这个模块因此只剩两件事：给出 :func:`sleight_home`，以及在第一次建库时把存量
``hosts.toml`` 读进来。**没有写入功能** —— 新的东西一律进库。
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import DeployError

__all__ = ["LegacyHost", "read_legacy_toml", "sleight_home"]

LEGACY_FILENAME = "hosts.toml"


def sleight_home() -> Path:
    """配置目录。``$SLEIGHT_HOME`` 优先 —— 单测靠它隔离，不去碰真的 home。"""
    if env := os.environ.get("SLEIGHT_HOME"):
        return Path(env).expanduser()
    return Path.home() / ".sleight"


@dataclass(frozen=True, slots=True)
class LegacyHost:
    """``hosts.toml`` 里的一条。只在导入时短暂存在。"""

    name: str
    ssh: str = ""
    port: int | None = None
    identity: str = ""
    sudo: bool = False
    strict_host_key: str = ""
    deploy: dict[str, Any] = field(default_factory=dict)


def read_legacy_toml(path: Path) -> list[LegacyHost]:
    """读旧的 ``hosts.toml``。

    :param path: 文件路径
    :returns: 里面的主机；文件不存在时是空列表
    :raises DeployError: 文件在但 TOML 语法错 —— 静默忽略会让人以为主机丢了
    """
    if not path.is_file():
        return []
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise DeployError(f"{path} is not valid TOML: {exc}") from exc

    hosts: list[LegacyHost] = []
    for name, table in (raw.get("hosts") or {}).items():
        if not isinstance(table, dict):
            continue
        deploy = table.get("deploy")
        hosts.append(LegacyHost(
            name=name,
            ssh=str(table.get("ssh", "") or ""),
            port=int(table["port"]) if table.get("port") else None,
            identity=str(table.get("identity", "") or ""),
            sudo=bool(table.get("sudo", False)),
            strict_host_key=str(table.get("strict_host_key", "") or ""),
            deploy=dict(deploy) if isinstance(deploy, dict) else {},
        ))
    return hosts
