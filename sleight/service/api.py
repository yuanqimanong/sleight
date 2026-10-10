"""Versioned APIs shared by the Web, Python clients and MCP."""

from __future__ import annotations

import asyncio
import platform
import uuid

from fastapi import Body, HTTPException, Request, WebSocket

from ..core import errors
from .database import database
from .execution import Supervisor


def setup_service(app, auth):
    supervisor = Supervisor()
    app.state.supervisor = supervisor
    identity, db = auth.identity, database()

    def user(request):
        principal = auth.resolve(request.headers, request.cookies)
        if not principal:
            raise HTTPException(401, "凭据无效或已失效")
        if "execute" not in principal.scopes and "manage" not in principal.scopes and not request.scope.get("path", "").endswith(("/api/v1/delegate", "/api/auth/me")):
            raise HTTPException(403, "此凭据仅可用于身份委托")
        return principal

    def admin(request):
        principal = user(request)
        if principal.role != "admin" or "manage" not in principal.scopes:
            raise HTTPException(403, "需要管理员管理权限")
        return principal

    def invoke(work):
        try:
            return work()
        except HTTPException:
            raise
        except Exception as exc:
            status = 403 if isinstance(exc, PermissionError) else 400 if isinstance(exc, ValueError) else \
                409 if isinstance(exc, errors.Busy) else 404 if isinstance(exc, errors.NotFound) else \
                410 if isinstance(exc, (errors.SessionLost, errors.LeaseLost, errors.StaleRef)) else \
                504 if isinstance(exc, (errors.TimeoutError, TimeoutError)) else 502
            # Upstream bodies may contain URLs or credentials; only local validation errors are shown.
            message = str(exc)[:500] if isinstance(exc, (ValueError, PermissionError, errors.Busy, errors.NotFound, errors.SessionLost, errors.LeaseLost, errors.StaleRef)) else "浏览器操作失败，请查看会话状态或重试连接"
            raise HTTPException(status, {"error": type(exc).__name__, "message": message}) from None

    @app.get("/api/auth/me")
    def me(request: Request):
        principal = user(request)
        return {"user": identity.user(principal.user_id), "scopes": principal.scopes, "api_version": 1,
                "storage": db.engine.dialect.name, "platform": {"os": platform.system(), "arch": platform.machine()}}

    @app.get("/api/v1/users")
    def users(request: Request):
        admin(request)
        return identity.users()

    @app.post("/api/v1/users")
    def save_user(request: Request, body: dict = Body(...)):
        admin(request)
        return invoke(lambda: identity.save_user(body))

    @app.get("/api/v1/tokens")
    def tokens(request: Request):
        principal = user(request)
        return identity.tokens(None if principal.role == "admin" and "manage" in principal.scopes else principal.user_id)

    @app.post("/api/v1/tokens")
    def issue(request: Request, body: dict = Body(...)):
        principal = user(request)
        uid = body.get("user_id") or principal.user_id
        scopes = body.get("scopes") or ["execute"]
        if set(scopes) - set(principal.scopes):
            raise HTTPException(403, "不能签发超出当前凭据权限的 token")
        if uid != principal.user_id:
            admin(request)
        return invoke(lambda: identity.issue(uid, name=body.get("name", "API"), ttl=max(0, int(body.get("ttl", 0))), scopes=scopes,
                      parent=identity.issuance_parent(principal.token_id)))

    @app.delete("/api/v1/tokens/{token_id}")
    def revoke(request: Request, token_id: str):
        principal = user(request)
        target = next((r for r in identity.tokens() if r["id"] == token_id), None)
        if not target or target["user_id"] != principal.user_id:
            admin(request)
        invoke(lambda: identity.revoke(token_id))
        return {"ok": True}

    @app.post("/api/v1/delegate")
    def delegate(request: Request, body: dict = Body(...)):
        principal = user(request)
        external = str(body.get("external_id", ""))
        if not external or len(external) > 160:
            raise HTTPException(400, "缺少有效外部用户标识")
        return invoke(lambda: identity.delegate(principal, external, str(body.get("name") or external), int(body.get("ttl", 900))))

    @app.get("/api/v1/environments")
    def environments(request: Request):
        from ..deploy.store import Store
        principal = user(request)
        default = identity.user(principal.user_id) or {}
        allowed = [default.get("default_environment"), *default.get("environments", [])]
        result = [{"id": "native", "name": "本机浏览器", "kind": "native", "limit": 3}]
        result.extend({"id": d.ref, "name": d.ref, "kind": "cloak", "limit": d.spec.max_running,
                       "ready": bool(d.deployed_at or d.status in ("deployed", "running", "imported"))} for d in Store().deployments())
        return [r for r in result if (principal.role == "admin" and "manage" in principal.scopes) or r["id"] in allowed]

    @app.get("/api/v1/profiles")
    def profiles(request: Request):
        principal = user(request)
        return [r for r in db.list("profiles") if r["owner"] == principal.user_id or (principal.role == "admin" and "manage" in principal.scopes)]

    @app.post("/api/v1/profiles")
    def create_profile(request: Request, body: dict = Body(...)):
        principal = user(request)
        if "execute" not in principal.scopes:
            raise HTTPException(403, "凭据没有执行权限")
        return invoke(lambda: supervisor.controller.create(body, principal, request.headers.get("idempotency-key", "")))

    @app.get("/api/v1/profiles/{pid}")
    def get_profile(request: Request, pid: str):
        return invoke(lambda: supervisor.controller.profile(pid, user(request)))

    @app.delete("/api/v1/profiles/{pid}")
    def delete_profile(request: Request, pid: str):
        principal = user(request)
        return invoke(lambda: supervisor.delete_profile(pid, principal))

    @app.get("/api/v1/sessions")
    def sessions(request: Request):
        principal = user(request)
        with db.state("service-sessions") as state:
            return [r for r in state["records"].values() if r["owner"] == principal.user_id or (principal.role == "admin" and "manage" in principal.scopes)]

    @app.post("/api/v1/sessions")
    def open_session(request: Request, body: dict = Body(...)):
        return invoke(lambda: supervisor.open(body, user(request), request.headers.get("idempotency-key") or uuid.uuid4().hex))

    @app.post("/api/v1/sessions/{sid}/heartbeat")
    def heartbeat(request: Request, sid: str):
        return invoke(lambda: supervisor.heartbeat(sid, user(request)))

    @app.post("/api/v1/sessions/{sid}/call")
    def call(request: Request, sid: str, body: dict = Body(...)):
        return invoke(lambda: supervisor.call(sid, user(request), body))

    @app.delete("/api/v1/sessions/{sid}")
    def close_session(request: Request, sid: str):
        return invoke(lambda: supervisor.close(sid, user(request)))

    @app.websocket("/api/v1/sessions/{sid}/events")
    async def events(socket: WebSocket, sid: str):
        principal = user(socket)
        invoke(lambda: supervisor.get(sid, principal))
        await socket.accept()
        try:
            while True:
                if not auth.resolve(socket.headers, socket.cookies):
                    await socket.close(code=4401)
                    return
                row = await asyncio.to_thread(supervisor.get, sid, principal)
                await socket.send_json({"state": row["state"], "expires": row["expires"]})
                if row["state"] == "released":
                    return
                await asyncio.sleep(2)
        except Exception:
            return

    @app.get("/api/v1/audit")
    def audit(request: Request):
        principal = user(request)
        with db.state("audit") as state:
            records = list(state["records"].values())
        return sorted((r for r in records if r["user"] == principal.user_id or (principal.role == "admin" and "manage" in principal.scopes)),
                      key=lambda r: r["time"], reverse=True)[:200]

    @app.post("/api/v1/collect")
    def collect(request: Request):
        admin(request)
        from ..deploy.api.runtime import sweep_managers
        supervisor.sweep()
        sweep_managers()
        return {"ok": True}

    from .console_api import setup_console
    setup_console(app, user, admin, invoke)
    from .mcp import setup_mcp
    setup_mcp(app, auth, supervisor)
