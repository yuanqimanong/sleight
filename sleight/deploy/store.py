"""本地 SQLite：主机、每台机上的若干 Manager、以及部署流水。

放在 ``~/.sleight/sleight.db``（``$SLEIGHT_HOME`` 可改）。用标准库 ``sqlite3``，
不引任何 ORM —— 这里就三张表。

**为什么从 TOML 换成 SQLite**：

* 一台机器可以跑**多个** Manager（不同目录、端口、容器名，preflight 还专门查
  ``/data`` 有没有被别的容器占着）。``hosts.toml`` 那种"一主机一份配置"的形状表达不了，
  于是"管理这些 manager"这件事根本无从谈起。
* 部署历史需要一个能追加、能查询、能并发写的地方。往 TOML 里追加流水是自找麻烦。
* Web 界面和 CLI 会同时开着。SQLite 有事务，两个进程一起写不会把配置写成半截。

**存量 ``hosts.toml`` 会在第一次用到时自动导入**，导入后原文件改名成 ``.imported``
留在原地（不删，万一你还想看）。

token 一如既往**不进这里** —— 每台机的 ``AUTH_TOKEN`` 在它自己的 ``.env``（600）里。
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .errors import DeployError
from .inventory import LEGACY_FILENAME, read_legacy_toml, sleight_home
from .runner import LocalRunner, Runner, SSHRunner
from .spec import DeploySpec

__all__ = ["Deployment", "Event", "Host", "Store", "namespaced", "store_path"]

FILENAME = "sleight.db"
SCHEMA_VERSION = 1

#: 部署名会被拼进 compose 项目名和容器名，所以得先满足它们的字符集
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS hosts (
    name             TEXT PRIMARY KEY,
    ssh              TEXT NOT NULL DEFAULT '',   -- 空 = 本机
    port             INTEGER,
    identity         TEXT NOT NULL DEFAULT '',
    sudo             INTEGER NOT NULL DEFAULT 0,
    strict_host_key  TEXT NOT NULL DEFAULT '',
    notes            TEXT NOT NULL DEFAULT '',
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS deployments (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    host         TEXT NOT NULL REFERENCES hosts(name) ON DELETE CASCADE,
    name         TEXT NOT NULL,
    spec         TEXT NOT NULL,                  -- DeploySpec 的 JSON
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    deployed_at  TEXT,                           -- 最后一次成功 apply
    image        TEXT NOT NULL DEFAULT '',       -- 最后一次实际部署上去的镜像
    status       TEXT NOT NULL DEFAULT '',       -- 最后一次看到的状态快照（JSON）
    UNIQUE (host, name)
);

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    host        TEXT NOT NULL,
    deployment  TEXT NOT NULL DEFAULT '',
    kind        TEXT NOT NULL,                   -- deploy / upgrade / backup / destroy / ext-apply …
    ok          INTEGER NOT NULL,
    detail      TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS events_recent ON events (ts DESC);
CREATE INDEX IF NOT EXISTS deployments_by_host ON deployments (host);
"""


def store_path() -> Path:
    return sleight_home() / FILENAME


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


# --------------------------------------------------------------------------- #
# 行
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Host:
    """一台目标机。``ssh`` 为空表示本机。"""

    name: str
    ssh: str = ""
    port: int | None = None
    identity: str = ""
    sudo: bool = False
    strict_host_key: str = ""
    notes: str = ""
    created_at: str = ""
    updated_at: str = ""

    @property
    def local(self) -> bool:
        return not self.ssh

    def runner(self, *, batch: bool = True) -> Runner:
        if self.local:
            return LocalRunner()
        return SSHRunner(
            self.ssh,
            port=self.port,
            identity=self.identity or None,
            batch=batch,
            strict_host_key=self.strict_host_key or None,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name, "ssh": self.ssh, "port": self.port,
            "identity": self.identity, "sudo": self.sudo,
            "strict_host_key": self.strict_host_key, "notes": self.notes,
            "local": self.local,
            "created_at": self.created_at, "updated_at": self.updated_at,
        }


@dataclass(frozen=True, slots=True)
class Deployment:
    """一台主机上的一个 Manager。"""

    host: str
    name: str
    spec: DeploySpec
    id: int = 0
    created_at: str = ""
    updated_at: str = ""
    deployed_at: str = ""
    image: str = ""
    status: dict[str, Any] = field(default_factory=dict)

    @property
    def ref(self) -> str:
        """``主机/部署名`` —— 命令行里指一个部署就用这个。"""
        return f"{self.host}/{self.name}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "host": self.host, "name": self.name, "ref": self.ref,
            "spec": self.spec.to_dict(),
            "created_at": self.created_at, "updated_at": self.updated_at,
            "deployed_at": self.deployed_at, "image": self.image, "status": self.status,
        }


@dataclass(frozen=True, slots=True)
class Event:
    """一条运维流水。"""

    ts: str
    host: str
    kind: str
    ok: bool
    deployment: str = ""
    detail: str = ""
    id: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "ts": self.ts, "host": self.host, "deployment": self.deployment,
            "kind": self.kind, "ok": self.ok, "detail": self.detail,
        }


# --------------------------------------------------------------------------- #
# Store
# --------------------------------------------------------------------------- #


class Store:
    """主机、部署、流水的持久化。

    :param path: 数据库文件。``None`` 用 :func:`store_path`；``":memory:"`` 给测试用
    """

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path is not None else store_path()
        self._memory = str(self.path) == ":memory:"
        self._lock = threading.Lock()
        self._shared: sqlite3.Connection | None = None
        if not self._memory:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self._connect() as conn:
            conn.executescript(SCHEMA)
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        self._import_legacy_toml()

    # ------------------------------------------------------------------ #

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """一次事务。

        ``:memory:`` 时复用同一个连接 —— 否则每次连接都是一个新的空库，测试里
        写进去的东西下一句就不见了。
        """
        with self._lock:
            if self._memory:
                if self._shared is None:
                    self._shared = sqlite3.connect(":memory:", check_same_thread=False)
                    self._shared.row_factory = sqlite3.Row
                    self._shared.execute("PRAGMA foreign_keys = ON")
                conn = self._shared
                with conn:
                    yield conn
                return
            conn = sqlite3.connect(self.path, timeout=10.0)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            # WAL：Web 界面和 CLI 常常同时开着，读不该被写堵住
            conn.execute("PRAGMA journal_mode = WAL")
            try:
                with conn:
                    yield conn
            finally:
                conn.close()

    def _import_legacy_toml(self) -> None:
        """把 0.2.x 的 ``hosts.toml`` 导进来。只做一次，原文件改名保留。"""
        legacy = sleight_home() / LEGACY_FILENAME
        try:
            hosts = read_legacy_toml(legacy)
        except DeployError:
            return                              # 语法坏了就别动它，让用户自己看
        if not hosts:
            return
        for old_host in hosts:
            if self.get_host(old_host.name) is not None:
                continue
            self.put_host(Host(
                name=old_host.name, ssh=old_host.ssh, port=old_host.port,
                identity=old_host.identity, sudo=old_host.sudo,
                strict_host_key=old_host.strict_host_key, notes="从 hosts.toml 导入",
            ))
            self.put_deployment(
                old_host.name, "default", DeploySpec.from_dict(old_host.deploy)
            )
        legacy.rename(legacy.with_suffix(".toml.imported"))

    # ------------------------------------------------------------------ #
    # 主机
    # ------------------------------------------------------------------ #

    def put_host(self, host: Host) -> Host:
        """新增或更新一台主机（按名字）。``created_at`` 只在新增时写。"""
        now = _now()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO hosts (name, ssh, port, identity, sudo, strict_host_key,
                                   notes, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (name) DO UPDATE SET
                    ssh = excluded.ssh, port = excluded.port, identity = excluded.identity,
                    sudo = excluded.sudo, strict_host_key = excluded.strict_host_key,
                    notes = excluded.notes, updated_at = excluded.updated_at
                """,
                (host.name, host.ssh, host.port, host.identity, int(host.sudo),
                 host.strict_host_key, host.notes, now, now),
            )
        return self.require_host(host.name)

    def get_host(self, name: str) -> Host | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM hosts WHERE name = ?", (name,)).fetchone()
        return _host(row) if row else None

    def require_host(self, name: str) -> Host:
        """:raises DeployError: 没这台机 —— 消息里列出有哪些，省一次 ``hosts ls``"""
        host = self.get_host(name)
        if host is None:
            known = ", ".join(h.name for h in self.hosts()) or "(还没配过)"
            raise DeployError(f"no host named {name!r} in {self.path}\n  known hosts: {known}")
        return host

    def hosts(self) -> list[Host]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM hosts ORDER BY name").fetchall()
        return [_host(r) for r in rows]

    def delete_host(self, name: str) -> None:
        """删主机，连带它的部署记录（**不动目标机上的任何东西**）。"""
        self.require_host(name)
        with self._connect() as conn:
            conn.execute("DELETE FROM deployments WHERE host = ?", (name,))
            conn.execute("DELETE FROM hosts WHERE name = ?", (name,))

    # ------------------------------------------------------------------ #
    # 部署
    # ------------------------------------------------------------------ #

    def put_deployment(self, host: str, name: str, spec: DeploySpec) -> Deployment:
        """新增或更新一台主机上的一个 Manager。

        名字没显式改过的话，会**按部署名自动加后缀**（见 :func:`namespaced`）——
        不然同一台机上的第二个 Manager 会把第一个删掉。

        :raises DeployError: 主机不存在、部署名不合法、spec 部署不了，或这台机上
            已经有别的部署占着同一个目录 / 项目名 / 容器名 / 端口
        """
        self.require_host(host)
        if not _NAME_RE.match(name):
            raise DeployError(
                f"deployment name {name!r} 不合法：只能用小写字母、数字、'_' 和 '-'，"
                "且不能以 '-' 开头（它会被拼进 compose 项目名和容器名）"
            )
        spec = namespaced(spec, name)
        spec.validate()
        self._refuse_collisions(host, name, spec)
        now = _now()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO deployments (host, name, spec, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (host, name) DO UPDATE SET
                    spec = excluded.spec, updated_at = excluded.updated_at
                """,
                (host, name, json.dumps(spec.to_dict(), ensure_ascii=False), now, now),
            )
        return self.require_deployment(host, name)

    def _refuse_collisions(self, host: str, name: str, spec: DeploySpec) -> None:
        """同一台机上，这四样每一样撞了都是事故。

        目录撞 → 两个 Manager 互相踩 profile 数据库。
        **compose 项目名撞 → 后部署的那个会把先部署的容器当成同项目的旧实例删掉**
        （真机上就是这么把第一个 Manager 静默干掉的）。
        容器名撞 → docker 直接拒绝创建。
        端口撞 → 起不来。
        """
        checks = (
            ("目录", "dir", spec.dir, "两个 Manager 共用一个数据目录会互相踩 profile 数据库"),
            ("compose 项目名", "name", spec.name,
             "后部署的会把先部署的容器当成同项目的旧实例删掉"),
            ("容器名", "container_name", spec.container_name, "docker 不允许重名容器"),
            ("端口", "port", spec.port, "端口只能被一个进程占用"),
        )
        for other in self.deployments(host=host):
            if other.name == name:
                continue
            for label, attr, value, why in checks:
                if getattr(other.spec, attr) == value:
                    raise DeployError(
                        f"{host} 上的部署 {other.name!r} 已经用着{label} {value!r} —— "
                        f"{why}。换一个再来。"
                    )

    def deployments(self, *, host: str | None = None) -> list[Deployment]:
        sql = "SELECT * FROM deployments"
        args: tuple[Any, ...] = ()
        if host is not None:
            sql += " WHERE host = ?"
            args = (host,)
        sql += " ORDER BY host, name"
        with self._connect() as conn:
            return [_deployment(r) for r in conn.execute(sql, args).fetchall()]

    def get_deployment(self, host: str, name: str) -> Deployment | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM deployments WHERE host = ? AND name = ?", (host, name)
            ).fetchone()
        return _deployment(row) if row else None

    def require_deployment(self, host: str, name: str) -> Deployment:
        found = self.get_deployment(host, name)
        if found is None:
            known = ", ".join(d.name for d in self.deployments(host=host)) or "(还没有)"
            raise DeployError(
                f"host {host!r} has no deployment named {name!r}\n  known: {known}"
            )
        return found

    def ensure_local(self) -> Deployment:
        """确保"本机"这条记录存在，返回它的 default 部署。

        Web 界面在主机列表里**总是**列一个本机（装了 docker 就能一键部署，不必先配
        主机）。那条是虚拟的，第一次真对它动手时才落库 —— 光打开界面不该往库里写。
        """
        if self.get_host("local") is None:
            self.put_host(Host(name="local", notes="本机"))
        if self.get_deployment("local", "default") is None:
            self.put_deployment("local", "default", DeploySpec())
        return self.require_deployment("local", "default")

    def resolve(self, ref: str) -> Deployment:
        """``主机`` 或 ``主机/部署名`` → 一个部署。

        只给主机名时：这台机只有一个部署就用它；有多个则报错并列出来 —— 猜一个
        然后往错的 Manager 上发命令是不能接受的。

        :raises DeployError: 找不到，或有歧义
        """
        host_name, _, deployment_name = ref.partition("/")
        self.require_host(host_name)
        if deployment_name:
            return self.require_deployment(host_name, deployment_name)

        found = self.deployments(host=host_name)
        if not found:
            raise DeployError(
                f"host {host_name!r} has no deployment yet — "
                f"sleight deployments add {host_name} --dir /srv/cloakbrowser-manager"
            )
        if len(found) > 1:
            names = ", ".join(f"{host_name}/{d.name}" for d in found)
            raise DeployError(
                f"host {host_name!r} has {len(found)} deployments, say which one: {names}"
            )
        return found[0]

    def delete_deployment(self, host: str, name: str) -> None:
        """从库里删掉这条记录。**不动目标机** —— 那是 ``sleight destroy`` 的事。"""
        self.require_deployment(host, name)
        with self._connect() as conn:
            conn.execute("DELETE FROM deployments WHERE host = ? AND name = ?", (host, name))

    def record_deploy(
        self, host: str, name: str, *, image: str, status: dict[str, Any] | None = None
    ) -> None:
        """记下"这个部署最后一次成功 apply 是什么时候、什么镜像"。"""
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE deployments
                   SET deployed_at = ?, image = ?, status = ?, updated_at = ?
                 WHERE host = ? AND name = ?
                """,
                (_now(), image, json.dumps(status or {}, ensure_ascii=False), _now(), host, name),
            )

    # ------------------------------------------------------------------ #
    # 流水
    # ------------------------------------------------------------------ #

    def log_event(
        self, host: str, kind: str, *, ok: bool, deployment: str = "", detail: str = ""
    ) -> None:
        """追加一条流水。**永远不抛** —— 记不上账不该让运维动作本身失败。"""
        try:
            with self._connect() as conn:
                conn.execute(
                    "INSERT INTO events (ts, host, deployment, kind, ok, detail) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (_now(), host, deployment, kind, int(ok), detail[:2000]),
                )
        except sqlite3.Error:                       # pragma: no cover - 磁盘满之类
            pass

    def events(self, *, host: str | None = None, limit: int = 50) -> list[Event]:
        sql = "SELECT * FROM events"
        args: list[Any] = []
        if host is not None:
            sql += " WHERE host = ?"
            args.append(host)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(max(1, limit))
        with self._connect() as conn:
            return [_event(r) for r in conn.execute(sql, args).fetchall()]

    def close(self) -> None:
        if self._shared is not None:
            self._shared.close()
            self._shared = None


# --------------------------------------------------------------------------- #


def namespaced(spec: DeploySpec, deployment: str) -> DeploySpec:
    """给非 default 的部署把 compose 项目名和容器名加上后缀。

    **不加的话同一台机上的第二个 Manager 会把第一个删掉** —— 两份 compose 渲染出
    同一个 ``name: cloakbrowser``，于是 ``docker compose up`` 认为对方的容器是本项目
    的旧实例，顺手清掉。真机上踩过。

    只在字段还是默认值时才动它：显式给了 ``--project`` / ``--container`` 就听你的。
    ``default`` 保持经典名字，这样单部署的机器升级上来什么都不用改。
    """
    if deployment == "default":
        return spec
    stock = DeploySpec()
    changes: dict[str, Any] = {}
    if spec.name == stock.name:
        changes["name"] = f"{stock.name}-{deployment}"
    if spec.container_name == stock.container_name:
        changes["container_name"] = f"{stock.container_name}-{deployment}"
    return spec.replace(**changes) if changes else spec


def _host(row: sqlite3.Row) -> Host:
    return Host(
        name=row["name"], ssh=row["ssh"], port=row["port"], identity=row["identity"],
        sudo=bool(row["sudo"]), strict_host_key=row["strict_host_key"], notes=row["notes"],
        created_at=row["created_at"], updated_at=row["updated_at"],
    )


def _deployment(row: sqlite3.Row) -> Deployment:
    return Deployment(
        id=row["id"], host=row["host"], name=row["name"],
        spec=DeploySpec.from_dict(json.loads(row["spec"])),
        created_at=row["created_at"], updated_at=row["updated_at"],
        deployed_at=row["deployed_at"] or "", image=row["image"],
        status=json.loads(row["status"]) if row["status"] else {},
    )


def _event(row: sqlite3.Row) -> Event:
    return Event(
        id=row["id"], ts=row["ts"], host=row["host"], deployment=row["deployment"],
        kind=row["kind"], ok=bool(row["ok"]), detail=row["detail"],
    )
