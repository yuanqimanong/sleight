"""One catalogue for service sessions, native profiles and imported Managers."""

from __future__ import annotations

import json
import time
import uuid
from contextlib import contextmanager

from sqlalchemy import select

from ..core import errors
from ..core.transport import Transport
from ..providers.plain import Plain
from ..runtime.capabilities import browser_capabilities
from ..runtime.inspector import inspect_browser
from ..runtime.native import NativeSupervisor
from .execution import manager_for


def is_admin(user):
    return user.role == "admin" and "manage" in user.scopes


class Fleet:
    def __init__(self, supervisor):
        self.supervisor = supervisor
        self.db = supervisor.db

    def sessions(self):
        with self.db.state("service-sessions") as state:
            return [dict(r) for r in state["records"].values() if r["state"] != "released"]

    def catalogue(self, user):
        from ..deploy.store import Store
        own = [r for r in self.db.list("profiles") if r["owner"] == user.user_id or is_admin(user)]
        bindings = {}
        for row in sorted(own, key=lambda r: (r.get("created", 0), r["id"]), reverse=True):
            if row.get("source_id"):
                bindings[(row["environment"], row["source_id"])] = row
        sessions = self.sessions()
        users = {r["id"]: r["name"] for r in self.supervisor.identity.users()}
        result, warnings = [], []
        native = {r["id"]: r for r in NativeSupervisor().list()}
        for source in native.values():
            row = bindings.pop(("native", source["id"]), None)
            if row or is_admin(user):
                row = row or {"environment": "native", "source_id": source["id"], "owner": "admin", "name": source["name"], "ephemeral": False}
                result.append(self.describe(row, source, sessions, users))
        environments = {r["environment"] for r in own if r["environment"] != "native"}
        if is_admin(user):
            environments.update(d.ref for d in Store().deployments() if d.deployed_at or d.status in ("deployed", "running", "imported"))
        for environment in sorted(environments):
            try:
                with manager_for(environment) as manager:
                    for source in manager.list_profiles():
                        row = bindings.pop((environment, source["id"]), None)
                        if row or is_admin(user):
                            row = row or {"environment": environment, "source_id": source["id"], "owner": "admin", "name": source["name"], "ephemeral": False, "external": True}
                            result.append(self.describe(row, source, sessions, users))
            except Exception:
                warnings.append(environment + " 暂时不可连接，实例状态保留为未知")
        for row in bindings.values():
            result.append(self.describe(row, {}, sessions, users))
        bound_ids = {r.get("id") for r in bindings.values()} | {r.get("profile_id") for r in result}
        for row in own:
            if not row.get("source_id") and row["id"] not in bound_ids:
                result.append(self.describe(row, {}, sessions, users))
        return {"instances": result, "warnings": warnings,
                "counts": {"all": len(result), "running": sum(r["status"] == "running" for r in result),
                           "persistent": sum(not r["ephemeral"] for r in result), "temporary": sum(r["ephemeral"] for r in result)}}

    def describe(self, row, source, sessions, users):
        native = row["environment"] == "native"
        caps = browser_capabilities(source.get("binary", "")) if native else {"kind": "cloak", "name": "Cloak", "version": source.get("browser_version", "")}
        sid = next((r for r in sessions if r.get("source_key") == row["environment"] + ":" + row["source_id"] or r["profile"] == row.get("id")), None)
        status = source.get("status", "unknown")
        if row.get("state") in ("creating", "cleanup_pending") or (sid and sid["state"] != "running"):
            status = row.get("state") if row.get("state") in ("creating", "cleanup_pending") else sid["state"]
        ref = "profile:" + row["id"] if row.get("id") else ("native:" + row["source_id"] if native else "manager:" + row["environment"] + ":" + row["source_id"])
        return {"ref": ref, "profile_id": row.get("id", ""), "id": row["source_id"] or row.get("id", ""),
                "name": row["name"], "kind": row.get("kind") or caps["kind"], "kernel": caps["name"], "version": caps.get("version", ""),
                "environment": row["environment"], "owner": row["owner"], "user_name": users.get(row["owner"], row["owner"]),
                "ephemeral": row["ephemeral"], "status": status, "external": row.get("external", False),
                "port": source.get("port") or source.get("cdp_port") or None, "pid": source.get("pid") or None,
                "memory_bytes": source.get("memory_bytes"), "notes": row.get("notes", source.get("notes") or ""),
                "session_id": sid["id"] if sid else "", "session_owner": sid["owner"] if sid else "",
                "created": row.get("created") or source.get("created_at"), "binary": source.get("binary", ""),
                "connection": {"api": "/api/v1", "profile_id": row.get("id", ""), "source_id": row["source_id"]}}

    def resolve(self, ref, user):
        if ref.startswith("profile:"):
            return self.supervisor.controller.profile(ref[8:], user)
        if not is_admin(user):
            raise errors.NotFound("实例不存在或没有权限")
        if ref.startswith("native:"):
            source = NativeSupervisor().get(ref[7:])
            return {"source_id": source["id"], "environment": "native", "name": source["name"], "ephemeral": False, "owner": user.user_id}
        if ref.startswith("manager:"):
            environment, _, pid = ref[8:].rpartition(":")
            with manager_for(environment) as manager:
                source = manager.get_profile(pid)
            return {"source_id": pid, "environment": environment, "name": source["name"], "ephemeral": False, "external": True, "owner": user.user_id}
        raise errors.NotFound("实例不存在")

    def bind(self, row, user):
        if row.get("id"):
            return row
        # A legacy source is registered only when explicitly operated, never on GET.
        from .database import ledger, objects
        with self.db.transaction() as conn:
            conn.execute(self.db.insert(ledger).values(resource="fleet-adoptions", body='{"records":{}}').on_conflict_do_nothing())
            state = json.loads(conn.execute(select(ledger.c.body).where(ledger.c.resource == "fleet-adoptions").with_for_update()).scalar_one())
            key = row["environment"] + ":" + row["source_id"]
            existing = state["records"].get(key)
            if existing:
                raw = conn.execute(select(objects.c.body).where(objects.c.scope == "profiles", objects.c.key == existing)).scalar_one_or_none()
                if raw:
                    return json.loads(raw)
            pid = uuid.uuid4().hex
            row = {**row, "id": pid, "state": "idle", "created": self.db.now(), "touched": self.db.now()}
            state["records"][key] = pid
            conn.execute(objects.insert().values(scope="profiles", key=pid, body=json.dumps(row)))
            conn.execute(ledger.update().where(ledger.c.resource == "fleet-adoptions").values(body=json.dumps(state)))
        return row

    def action(self, ref, action, user):
        row = self.bind(self.resolve(ref, user), user)
        if action not in ("start", "stop", "restart"):
            raise ValueError("未知实例操作")
        if row["ephemeral"] and action == "restart":
            raise ValueError("临时实例不支持重启；停止回收后请新建实例，以便生成新身份")
        active = next((s for s in self.sessions() if s.get("source_key") == row["environment"] + ":" + row["source_id"] or s["profile"] == row["id"]), None)
        if action in ("stop", "restart"):
            if active:
                self.supervisor.close(active["id"], user)
                if self.supervisor.get(active["id"], user)["state"] != "released":
                    raise errors.Busy("实例仍在回收，请等待后重试")
            else:
                self.supervisor.controller.cleanup(row, delete=row["ephemeral"])
            self.record(row["id"], action, "临时实例已回收" if row["ephemeral"] else "浏览器已停止，长期实例保留登录态")
            if row["ephemeral"]:
                return {"ok": True, "deleted": True, "profile_id": row["id"]}
        result = {"ok": True, "profile_id": row["id"]}
        if action in ("start", "restart"):
            if active and action == "start":
                raise errors.Busy("实例已有运行会话")
            session = self.supervisor.open({"profile": row["id"], "options": {"human": True}}, user, uuid.uuid4().hex)
            result["session_id"] = session["id"]
            self.record(row["id"], action, "浏览器会话已启动")
        return result

    @contextmanager
    def transport(self, row):
        if row["environment"] == "native":
            native = NativeSupervisor().get(row["source_id"])
            if native["status"] != "running":
                raise errors.NotReady("实例未运行")
            endpoint = Plain(f"http://127.0.0.1:{native['port']}").endpoint()
            with Transport.connect(endpoint.ws_url) as transport:
                yield transport
        else:
            with manager_for(row["environment"]) as manager:
                endpoint = manager.endpoint(row["source_id"])
                with Transport.connect(endpoint.ws_url, headers=endpoint.headers) as transport:
                    yield transport

    def inspect(self, ref, user, **options):
        row = self.resolve(ref, user)
        session = next((s for s in self.sessions() if s["state"] == "running" and (s["profile"] == row.get("id") or s.get("source_key") == row["environment"] + ":" + row["source_id"])), None)
        if session:
            return self.supervisor.call(session["id"], user, {"method": "$inspect", "kwargs": options})
        with self.transport(row) as transport:
            return inspect_browser(transport, **options)

    def remove(self, ref, user, confirm):
        row = self.resolve(ref, user)
        if confirm != row["name"]:
            raise ValueError("请输入实例名称确认删除")
        if not row.get("id") and row.get("external"):
            raise ValueError("此实例由外部 Manager 管理，请在官方管理页删除原 profile")
        row = self.bind(row, user)
        return self.supervisor.delete_profile(row["id"], user)

    def record(self, pid, action, message):
        with self.db.state("fleet-logs") as state:
            entries = state["records"].setdefault(pid, [])
            entries.append({"time": time.time(), "action": action, "message": message})
            state["records"][pid] = entries[-200:]
            if len(state["records"]) > 2000:
                state["records"].pop(next(iter(state["records"])))

    def logs(self, ref, user):
        row = self.resolve(ref, user)
        with self.db.state("fleet-logs") as state:
            entries = state["records"].get(row.get("id", ""), [])
        sessions = [s for s in self.sessions() if s["profile"] == row.get("id")]
        return {"name": row["name"], "entries": entries, "sessions": [{"id": s["id"], "state": s["state"], "created": s["created"]} for s in sessions]}
