"""Encrypted bootstrap settings and explicit, quiescent database migration."""

from __future__ import annotations

import json
import os
import re
import threading
import uuid

from cryptography.fernet import Fernet
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import URL, make_url


def configured_url(root):
    if value := os.environ.get("SLEIGHT_DATABASE_URL"):
        return value
    path = root / "storage.json"
    if path.is_file():
        from .identity import encryption_key
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("format") != 1:
            raise ValueError("数据库启动配置版本不支持")
        return Fernet(encryption_key(root)).decrypt(payload["url"].encode()).decode()
    return "sqlite:///" + str(root / "control.db")


def public_connection(url):
    value = make_url(url)
    return {"backend": "postgresql" if value.drivername.startswith("postgresql") else "sqlite", "host": value.host or "", "port": value.port or 5432,
            "username": value.username or "", "database": value.database or "", "password_configured": bool(value.password),
            "schema": str(value.query.get("options", "")).removeprefix("-csearch_path="), "sslmode": value.query.get("sslmode", "prefer")}


def normalized_url(url):
    value = make_url(url)
    return value.set(drivername="postgresql+psycopg") if value.drivername.startswith("postgresql") else value


class StorageSettings:
    def __init__(self, db):
        self.db, self.root = db, db.root

    def status(self):
        pending = configured_url(self.root)
        active = self.db.url.replace("postgresql+psycopg://", "postgresql://", 1)
        return {"current": public_connection(active), "pending": public_connection(pending), "restart_required": normalized_url(active) != normalized_url(pending),
                "environment_override": bool(os.environ.get("SLEIGHT_DATABASE_URL")), "data_dir": str(self.root.resolve()),
                "key_path": str((self.root / "secret.key").resolve())}

    def connection(self, body):
        backend = body.get("backend", "postgresql")
        if backend == "sqlite":
            return "sqlite:///" + str(self.root / ("control-" + uuid.uuid4().hex[:8] + ".db"))
        host, user, name = (str(body.get(k, "")).strip() for k in ("host", "username", "database"))
        if not host or not user or not name or any("\n" in v or "\r" in v for v in (host, user, name)):
            raise ValueError("请填写 PostgreSQL 主机、用户名和数据库名")
        schema = str(body.get("schema", "")).strip()
        if schema and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,62}", schema):
            raise ValueError("schema 只支持字母、数字和下划线，且不能以数字开头")
        query = {"sslmode": str(body.get("sslmode", "prefer")), "connect_timeout": "5"}
        if query["sslmode"] not in ("disable", "prefer", "require", "verify-ca", "verify-full"):
            raise ValueError("SSL 模式无效")
        if schema:
            query["options"] = "-csearch_path=" + schema
        return URL.create("postgresql+psycopg", username=user, password=str(body.get("password", "")), host=host,
                          port=int(body.get("port") or 5432), database=name, query=query).render_as_string(hide_password=False)

    def test(self, body):
        if body.get("backend") == "sqlite":
            return {"ok": True, "message": "SQLite 无需服务器连接；迁移会新建文件并保留原数据库"}
        url = self.connection(body)
        engine = create_engine(url, pool_pre_ping=True)
        try:
            with engine.connect() as connection:
                connection.execute(text("SELECT 1"))
                schema = str(body.get("schema", "")).strip()
                if schema and not connection.execute(text("SELECT 1 FROM pg_namespace WHERE nspname=:name"), {"name": schema}).scalar():
                    raise ValueError("schema 不存在，请先创建专用 schema")
            return {"ok": True, "message": "连接成功；迁移时只接受空目标数据库或空 schema"}
        except ValueError:
            raise
        except Exception:
            raise ValueError("数据库连接失败，请检查地址、端口、账号、SSL 和网络；连接信息不会写入日志") from None
        finally:
            engine.dispose()

    def migrate(self, body):
        if os.environ.get("SLEIGHT_DATABASE_URL"):
            raise ValueError("数据库由 SLEIGHT_DATABASE_URL 控制，请先移除环境变量，再配置 Web 启动设置")
        if self.db.storage_pending:
            raise ValueError("数据库已等待切换，请先重启 Sleight")
        self.test(body)
        url = self.connection(body)
        if normalized_url(url) == normalized_url(self.db.url):
            raise ValueError("目标与当前数据库相同")
        engine = create_engine(url)
        try:
            with engine.connect() as connection:
                if inspect(connection).get_table_names() or inspect(connection).get_view_names():
                    raise ValueError("目标包含表或视图，请使用独立空数据库或空 schema")
        finally:
            engine.dispose()
        with self.db.storage_lock:
            if self.db.storage_pending:
                raise ValueError("数据库已等待切换，请先重启 Sleight")
            self.db.storage_pending = True
            self.db.storage_writer = threading.get_ident()
            try:
                return self._migrate(url)
            except BaseException:
                self.db.storage_pending = False
                raise
            finally:
                self.db.storage_writer = None

    def _migrate(self, url):
        from .backup import export_data, import_data
        from .database import Database
        from .identity import encryption_key
        backup = self.root / "backups" / ("database-" + uuid.uuid4().hex + ".json")
        backup.parent.mkdir(parents=True, exist_ok=True)
        export_data(backup, db=self.db)
        target = Database(url, import_legacy=False)
        try:
            import_data(backup, db=target)
        finally:
            target.engine.dispose()
        payload = {"format": 1, "url": Fernet(encryption_key(self.root)).encrypt(url.encode()).decode()}
        path, temporary = self.root / "storage.json", self.root / (".storage-" + uuid.uuid4().hex)
        if path.exists():
            previous = self.root / "storage.previous.json"
            previous.write_bytes(path.read_bytes())
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream)
        temporary.replace(path)
        return {"ok": True, "restart_required": True, "backup": str(backup.resolve()),
                "message": "数据已迁移，写入已暂停；请停止并重新启动 Sleight。回滚可移走 storage.json，原数据库和备份均保留。"}
