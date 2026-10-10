"""Portable, versioned data export. Quiesce the service before switching storage."""

import json
import os
from pathlib import Path

from sqlalchemy import select

from .database import Database, database, metadata


def export_data(path, *, db=None):
    db = db or database()
    with db.transaction() as conn:
        payload = {table.name: [dict(r) for r in conn.execute(select(table)).mappings()]
                   for table in metadata.sorted_tables}
        for row in payload["ledger"]:
            value = json.loads(row["body"])
            if row["resource"] == "profile-requests" and any(r.get("state") == "creating" for r in value.get("records", {}).values()):
                raise ValueError("实例仍在创建，请等待完成后迁移数据库")
            if row["resource"] == "service-sessions" and value.get("deletions"):
                raise ValueError("实例删除尚未完成，不能迁移运行状态")
            if row["resource"] == "service-sessions" and any(r["state"] != "released" for r in value.get("records", {}).values()):
                raise ValueError("请先停止浏览器会话并完成回收，再导出数据库")
            if row["resource"] == "native" and any(r.get("pid") for r in value.get("records", {}).values()):
                raise ValueError("请先停止本机浏览器，再导出数据库")
            records = value.get("records", {})
            if (row["resource"] == "instance-leases" and records) or any(isinstance(r, dict) and r.get("slot") for r in records.values()):
                raise ValueError("浏览器额度或租约尚未回收，不能迁移运行状态")
        if any(r.get("status") == "running" for r in map(lambda r: json.loads(r["body"]), payload["objects"]) if isinstance(r, dict)):
            raise ValueError("请先等待后台任务完成，再导出数据库")
    target = Path(path)
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump({"format": 1, "tables": payload}, stream, ensure_ascii=False)
    return {"path": str(target.resolve()), "rows": {k: len(v) for k, v in payload.items()},
            "key_file": str(db.root / "secret.key")}


def import_data(path, *, db=None):
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if payload.get("format") != 1 or set(payload.get("tables", {})) != set(metadata.tables):
        raise ValueError("备份格式或表结构不兼容")
    owned = db is None
    db = db or Database(import_legacy=False)
    try:
        return _restore(db, payload)
    finally:
        if owned:
            db.engine.dispose()


def _restore(db, payload):
    with db.transaction() as conn:
        for table in metadata.sorted_tables:
            rows = conn.execute(select(table)).mappings().all()
            if rows and not (table.name == "objects" and all(r["scope"] == "schema" for r in rows)):
                raise ValueError("只能导入空数据库；请为目标配置独立数据库和 SLEIGHT_HOME")
        for table in reversed(metadata.sorted_tables):
            conn.execute(table.delete())
        for table in metadata.sorted_tables:
            for row in payload["tables"][table.name]:
                conn.execute(table.insert().values(**row))
        if db.engine.dialect.name == "postgresql":
            from sqlalchemy import text
            for name in ("deployments", "events"):
                conn.execute(text(f"SELECT setval(pg_get_serial_sequence('{name}','id'), COALESCE(MAX(id),1), COUNT(*) > 0) FROM {name}"))
    return {"ok": True, "message": "启动服务前复制原 secret.key，或设置相同 SLEIGHT_SECRET_KEY"}
