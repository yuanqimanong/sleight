"""One transactional store for SQLite and PostgreSQL; no Redis is required."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from functools import wraps
from pathlib import Path
from typing import Any

from sqlalchemy import (
    Column,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    create_engine,
    event,
    select,
    text,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.pool import StaticPool

metadata = MetaData()
hosts = Table("hosts", metadata, Column("name", String, primary_key=True),
              Column("ssh", Text, nullable=False, server_default=""), Column("port", Integer),
              Column("identity", Text, nullable=False, server_default=""),
              Column("sudo", Integer, nullable=False, server_default="0"),
              Column("strict_host_key", Text, nullable=False, server_default=""),
              Column("notes", Text, nullable=False, server_default=""),
              Column("created_at", Text, nullable=False), Column("updated_at", Text, nullable=False))
deployments = Table("deployments", metadata, Column("id", Integer, primary_key=True),
                    Column("host", String, ForeignKey("hosts.name", ondelete="CASCADE"), nullable=False),
                    Column("name", String, nullable=False), Column("spec", Text, nullable=False),
                    Column("created_at", Text, nullable=False), Column("updated_at", Text, nullable=False),
                    Column("deployed_at", Text), Column("image", Text, nullable=False, server_default=""),
                    Column("status", Text, nullable=False, server_default=""), UniqueConstraint("host", "name"))
events = Table("events", metadata, Column("id", Integer, primary_key=True),
               Column("ts", Text, nullable=False), Column("host", Text, nullable=False),
               Column("deployment", Text, nullable=False, server_default=""), Column("kind", Text, nullable=False),
               Column("ok", Integer, nullable=False), Column("detail", Text, nullable=False, server_default=""))
ledger = Table("ledger", metadata, Column("resource", String, primary_key=True), Column("body", Text, nullable=False))
objects = Table("objects", metadata, Column("scope", String, primary_key=True),
                Column("key", String, primary_key=True), Column("body", Text, nullable=False))


def storage_write(fn):
    """Keep a side effect and its durable record together during storage switches."""
    @wraps(fn)
    def call(self, *args, **kwargs):
        db = self.db if hasattr(self, "db") else self.ledger.db
        with db.storage_lock:
            if db.storage_pending:
                from ..core.errors import Busy
                raise Busy("数据库正在切换，请重启 Sleight 后继续操作")
            return fn(self, *args, **kwargs)
    return call


class Row(dict):
    def __getitem__(self, key):
        return list(self.values())[key] if isinstance(key, int) else super().__getitem__(key)


class Result:
    def __init__(self, result):
        self.result = result

    def fetchone(self):
        value = self.result.mappings().first()
        return Row(value) if value is not None else None

    def fetchall(self):
        return [Row(r) for r in self.result.mappings().all()]


class Connection:
    """Small DB-API facade for existing deployment queries."""

    def __init__(self, conn):
        self.conn = conn

    def execute(self, sql, args=()):
        parts = sql.split("?")
        if len(parts) != len(args) + 1:
            raise ValueError("SQL parameter count mismatch")
        statement = parts[0] + "".join(f":p{i}" + part for i, part in enumerate(parts[1:]))
        try:
            return Result(self.conn.execute(text(statement), {f"p{i}": v for i, v in enumerate(args)}))
        except IntegrityError as exc:
            raise sqlite3.IntegrityError("Database constraint rejected the operation") from exc


class Database:
    def __init__(self, url: str | None = None, *, import_legacy: bool = True):
        from ..deploy.inventory import sleight_home
        self.root = sleight_home()
        self.storage_lock = threading.RLock()
        self.storage_pending = False
        self.storage_writer = None
        from .storage_config import configured_url
        self.url = url or configured_url(self.root)
        if self.url.startswith("postgresql://"):
            self.url = self.url.replace("postgresql://", "postgresql+psycopg://", 1)
        if not self.url.startswith(("sqlite:", "postgresql+psycopg:")):
            raise ValueError("SLEIGHT_DATABASE_URL must use SQLite or PostgreSQL")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        kw: dict[str, Any] = {"pool_pre_ping": True}
        if self.url.startswith("sqlite:"):
            kw["connect_args"] = {"check_same_thread": False, "timeout": 30}
            if self.url.endswith(":memory:"):
                kw["poolclass"] = StaticPool
            else:
                from sqlalchemy.engine import make_url
                Path(make_url(self.url).database).parent.mkdir(parents=True, exist_ok=True)
        self.engine = create_engine(self.url, **kw)
        if self.engine.dialect.name == "sqlite":
            @event.listens_for(self.engine, "connect")
            def configure(conn, _):
                conn.execute("PRAGMA foreign_keys=ON")
        with self.transaction() as conn:
            if self.engine.dialect.name == "postgresql":
                conn.execute(text("SELECT pg_advisory_xact_lock(1397508428)"))
            metadata.create_all(conn)
        if self.engine.dialect.name == "sqlite":
            with self.engine.connect() as conn:
                # Concurrent initializers may momentarily hold the schema lock.
                for attempt in range(30):
                    try:
                        conn.exec_driver_sql("PRAGMA journal_mode=WAL")
                        break
                    except Exception:
                        if attempt == 29:
                            raise
                        time.sleep(.1)
        if import_legacy:
            self._import_legacy()
        version = self.get("schema", "version")
        if version not in (None, 1):
            raise ValueError("Unsupported Sleight database schema; restore the matching service version")
        if version is None:
            self.put("schema", "version", 1)

    def insert(self, table):
        return pg_insert(table) if self.engine.dialect.name == "postgresql" else sqlite_insert(table)

    def now(self):
        sql = "SELECT EXTRACT(EPOCH FROM clock_timestamp())" if self.engine.dialect.name == "postgresql" else "SELECT (julianday('now') - 2440587.5) * 86400.0"
        with self.engine.connect() as conn:
            return float(conn.execute(text(sql)).scalar_one())

    @contextmanager
    def transaction(self):
        with self.storage_lock:
            if self.storage_pending and self.storage_writer != threading.get_ident():
                from ..core.errors import Busy
                raise Busy("数据库正在切换，请重启 Sleight 后继续操作")
            with self._transaction() as conn:
                yield conn

    @contextmanager
    def _transaction(self):
        with self.engine.connect() as conn:
            if self.engine.dialect.name == "sqlite":
                conn.exec_driver_sql("BEGIN IMMEDIATE")
            else:
                conn.begin()
            try:
                yield conn
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

    @contextmanager
    def state(self, key: str) -> Iterator[dict]:
        if self.storage_pending and self.storage_writer != threading.get_ident():
            with self.engine.connect() as conn:
                raw = conn.execute(select(ledger.c.body).where(ledger.c.resource == key)).scalar_one_or_none()
            yield json.loads(raw) if raw else {"records": {}, "owners": {}}
            return
        with self.transaction() as conn:
            conn.execute(self.insert(ledger).values(resource=key, body='{"records":{},"owners":{}}')
                         .on_conflict_do_nothing(index_elements=[ledger.c.resource]))
            row = conn.execute(select(ledger.c.body).where(ledger.c.resource == key).with_for_update()).scalar_one()
            state = json.loads(row)
            yield state
            conn.execute(ledger.update().where(ledger.c.resource == key).values(body=json.dumps(state)))

    def get(self, scope: str, key: str, default=None):
        with self.engine.connect() as conn:
            raw = conn.execute(select(objects.c.body).where(objects.c.scope == scope, objects.c.key == key)).scalar_one_or_none()
        return json.loads(raw) if raw is not None else default

    def put(self, scope: str, key: str, value):
        with self.transaction() as conn:
            statement = self.insert(objects).values(scope=scope, key=key, body=json.dumps(value, default=str))
            conn.execute(statement.on_conflict_do_update(index_elements=[objects.c.scope, objects.c.key],
                                                         set_={"body": statement.excluded.body}))

    def delete(self, scope: str, key: str):
        with self.transaction() as conn:
            conn.execute(objects.delete().where(objects.c.scope == scope, objects.c.key == key))

    def list(self, scope: str):
        with self.engine.connect() as conn:
            return [json.loads(v) for v in conn.execute(select(objects.c.body).where(objects.c.scope == scope)).scalars()]

    def _import_legacy(self):
        if not self.get("migration", "deployed", False):
            rows = {}
            path = self.root / "sleight.db"
            if path.is_file():
                with sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True) as source:
                    source.row_factory = sqlite3.Row
                    for table in (hosts, deployments, events):
                        if source.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table.name,)).fetchone():
                            rows[table.name] = [dict(r) for r in source.execute(f"SELECT * FROM {table.name}")]
            with self.transaction() as conn:
                for table in (hosts, deployments, events):
                    for row in rows.get(table.name, []):
                        conn.execute(self.insert(table).values(**row).on_conflict_do_nothing())
                if self.engine.dialect.name == "postgresql":
                    for table in (deployments, events):
                        conn.execute(text(f"SELECT setval(pg_get_serial_sequence('{table.name}','id'), COALESCE(MAX(id),1), COUNT(*) > 0) FROM {table.name}"))
                conn.execute(self.insert(objects).values(scope="migration", key="deployed", body="true")
                             .on_conflict_do_nothing())
        for filename, table, scope in (("runtime.db", "ledger", ""), ("jobs.db", "jobs", "jobs")):
            if self.get("migration", filename):
                continue
            path = self.root / filename
            if path.is_file():
                with sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True) as source:
                    if source.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone():
                        for key, raw in source.execute(f"SELECT * FROM {table}"):
                            if scope:
                                self.put(scope, key, json.loads(raw))
                            else:
                                with self.transaction() as conn:
                                    conn.execute(self.insert(ledger).values(resource=key, body=raw)
                                                 .on_conflict_do_nothing())
            self.put("migration", filename, True)


_databases: dict[tuple[str, str], Database] = {}
_lock = threading.RLock()


def database() -> Database:
    from ..deploy.inventory import sleight_home
    key = (str(sleight_home().resolve()), os.environ.get("SLEIGHT_DATABASE_URL", ""))
    with _lock:
        if key not in _databases:
            _databases[key] = Database()
        return _databases[key]
