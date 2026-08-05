"""``~/.sleight/hosts.toml`` —— 给目标机起个名字。

有它之后 ``sleight status --host hk-01`` 就够了，不用每次重复 ssh 地址、私钥路径、
部署目录。

**不存 token。** 每台机的 ``AUTH_TOKEN`` 就放在它自己的 ``.env``（权限 600）里 ——
能 SSH 上去就能读到它，再抄一份到控制机只是多一个泄漏点。要用的时候
``sleight token --host hk-01`` 现取。
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import DeployError
from .runner import LocalRunner, Runner, SSHRunner
from .spec import DeploySpec

__all__ = ["Host", "Inventory", "sleight_home"]

FILENAME = "hosts.toml"


def sleight_home() -> Path:
    """配置目录。``$SLEIGHT_HOME`` 优先 —— 单测靠它隔离，不去碰真的 home。"""
    if env := os.environ.get("SLEIGHT_HOME"):
        return Path(env).expanduser()
    return Path.home() / ".sleight"


@dataclass(frozen=True, slots=True)
class Host:
    """一台目标机。

    :param ssh: ``user@host`` 或 ``~/.ssh/config`` 里的别名。**空串表示本机**
    :param deploy: :class:`~sleight.deploy.spec.DeploySpec` 的字段覆盖。放成一个子表
        而不是摊平，这样 spec 加字段时这里不用改
    """

    name: str
    ssh: str = ""
    port: int | None = None
    identity: str = ""
    sudo: bool = False
    strict_host_key: str = ""
    deploy: dict[str, Any] = field(default_factory=dict)

    @property
    def local(self) -> bool:
        return not self.ssh

    def runner(self, *, batch: bool = True) -> Runner:
        """建一个能在这台机器上干活的 Runner。"""
        if self.local:
            return LocalRunner()
        return SSHRunner(
            self.ssh,
            port=self.port,
            identity=self.identity or None,
            batch=batch,
            strict_host_key=self.strict_host_key or None,
        )

    def spec(self, **overrides: Any) -> DeploySpec:
        """这台机的部署描述。``overrides`` 里 ``None`` 的值会被忽略，所以命令行没给的
        参数不会把配置文件里的值打掉。"""
        data = {**self.deploy, **{k: v for k, v in overrides.items() if v is not None}}
        return DeploySpec.from_dict(data)

    def to_toml_table(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if self.ssh:
            out["ssh"] = self.ssh
        if self.port:
            out["port"] = self.port
        if self.identity:
            out["identity"] = self.identity
        if self.sudo:
            out["sudo"] = True
        if self.strict_host_key:
            out["strict_host_key"] = self.strict_host_key
        if self.deploy:
            out["deploy"] = dict(self.deploy)
        return out


class Inventory:
    """``hosts.toml`` 的读写。

    读用标准库 ``tomllib``；写用下面那个只认字符串/整数/布尔/单层子表的极简序列化器 ——
    标准库没有 TOML writer，而为了写这么几行去背一个依赖不划算。
    """

    def __init__(self, hosts: dict[str, Host] | None = None, *, path: Path | None = None) -> None:
        self.hosts = dict(hosts or {})
        self.path = path or (sleight_home() / FILENAME)

    # ------------------------------------------------------------------ #

    @classmethod
    def load(cls, path: Path | None = None) -> Inventory:
        """读配置。文件不存在就是空清单，不报错。

        :raises DeployError: 文件存在但 TOML 语法错 —— 静默忽略会让人以为主机没配上
        """
        target = path or (sleight_home() / FILENAME)
        if not target.is_file():
            return cls(path=target)
        try:
            raw = tomllib.loads(target.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as exc:
            raise DeployError(f"{target} is not valid TOML: {exc}") from exc

        hosts: dict[str, Host] = {}
        for name, table in (raw.get("hosts") or {}).items():
            if not isinstance(table, dict):
                continue
            deploy = table.get("deploy")
            hosts[name] = Host(
                name=name,
                ssh=str(table.get("ssh", "") or ""),
                port=int(table["port"]) if table.get("port") else None,
                identity=str(table.get("identity", "") or ""),
                sudo=bool(table.get("sudo", False)),
                strict_host_key=str(table.get("strict_host_key", "") or ""),
                deploy=dict(deploy) if isinstance(deploy, dict) else {},
            )
        return cls(hosts, path=target)

    def save(self) -> Path:
        """写回配置。目录不存在会建，权限 700。"""
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        text = "# sleight 的目标机清单。sleight hosts add 会写这里，手改也行。\n"
        text += "# 这里没有 token —— 各机的 AUTH_TOKEN 在它自己的 .env 里。\n"
        for name in sorted(self.hosts):
            text += f"\n[hosts.{_key(name)}]\n"
            table = self.hosts[name].to_toml_table()
            nested = table.pop("deploy", None)
            text += _dump(table)
            if nested:
                text += f"\n[hosts.{_key(name)}.deploy]\n" + _dump(nested)
        self.path.write_text(text, encoding="utf-8")
        return self.path

    # ------------------------------------------------------------------ #

    def get(self, name: str) -> Host:
        """按名字取。

        :raises DeployError: 没这台机 —— 消息里列出有哪些，省一次 ``hosts ls``
        """
        try:
            return self.hosts[name]
        except KeyError:
            known = ", ".join(sorted(self.hosts)) or "(none configured yet)"
            raise DeployError(
                f"no host named {name!r} in {self.path}\n  known hosts: {known}"
            ) from None

    def add(self, host: Host) -> None:
        self.hosts[host.name] = host

    def remove(self, name: str) -> None:
        self.get(name)
        del self.hosts[name]


# --------------------------------------------------------------------------- #


def _key(name: str) -> str:
    """TOML 的裸键只允许字母数字下划线连字符，其它一律加引号。"""
    if name and all(c.isalnum() or c in "_-" for c in name):
        return name
    escaped = name.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _dump(table: dict[str, Any]) -> str:
    lines = []
    for key, value in table.items():
        if isinstance(value, bool):
            lines.append(f"{_key(key)} = {'true' if value else 'false'}")
        elif isinstance(value, int):
            lines.append(f"{_key(key)} = {value}")
        elif isinstance(value, (list, tuple)):
            items = ", ".join(_scalar(v) for v in value)
            lines.append(f"{_key(key)} = [{items}]")
        else:
            lines.append(f"{_key(key)} = {_scalar(value)}")
    return "".join(f"{ln}\n" for ln in lines)


def _scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    text = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{text}"'
