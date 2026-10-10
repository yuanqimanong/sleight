"""Header authentication for services, short-lived HttpOnly sessions for browsers."""

from __future__ import annotations

import secrets
import time
from urllib.parse import urlsplit

from fastapi import HTTPException, Request, Response
from starlette.requests import HTTPConnection

from ...service.identity import Identity, Principal


class UIAuth:
    def __init__(self, token: str | None, *, required=False) -> None:
        self.token = token
        self.required = required or bool(token)
        self.identity = Identity()
        if token:
            self.identity.ensure_admin(token)
        self.sessions: dict[str, float] = {}
        self.failures: dict[str, list[float]] = {}

    def valid(self, headers, cookies) -> bool:
        return self.resolve(headers, cookies) is not None

    def resolve(self, headers, cookies):
        raw = headers.get("x-sleight-token", "") or headers.get("authorization", "").removeprefix("Bearer ")
        raw = raw or cookies.get("sleight_session", "")
        if raw:
            return self.identity.lookup(raw)
        return Principal("admin", "admin", scopes=("execute", "manage", "delegate")) if not self.required else None

    def origin(self, headers) -> None:
        origin = headers.get("origin")
        if origin and urlsplit(origin).netloc != headers.get("host"):
            raise HTTPException(403, "Cross-origin management request refused")

    def guard(self, request: HTTPConnection) -> None:
        path = request.scope.get("path", "")
        root = request.scope.get("root_path", "")
        if root and path.startswith(root):
            path = path[len(root):]
        if path in ("/", "/api/auth/login") or path.startswith("/assets/"):
            return
        if not self.valid(request.headers, request.cookies):
            raise HTTPException(401, "bad or missing UI token")
        principal = self.resolve(request.headers, request.cookies)
        request.state.principal = principal
        if not set(principal.scopes) & {"execute", "manage"} and path not in ("/api/auth/me", "/api/v1/delegate"):
            raise HTTPException(403, "此凭据仅可用于身份委托")
        personal = path.startswith(("/api/v1/", "/api/config/templates", "/api/auth/me", "/mcp", "/api/auth/logout"))
        if not personal and (principal.role != "admin" or "manage" not in principal.scopes):
            raise HTTPException(403, "此功能需要管理员管理权限")
        if request.scope["type"] == "websocket" or request.scope.get("method", "GET") not in ("GET", "HEAD", "OPTIONS"):
            self.origin(request.headers)
            if self.identity.db.storage_pending and path != "/api/auth/logout":
                raise HTTPException(409, "数据库正在切换，请重启 Sleight 后继续操作")

    def login(self, request: Request, response: Response, token: str) -> dict:
        self.origin(request.headers)
        address = request.client.host if request.client else "unknown"
        now = time.time()
        recent = [t for t in self.failures.get(address, []) if t > now - 60]
        if len(recent) >= 10:
            raise HTTPException(429, "Too many login attempts; retry in one minute")
        principal = self.identity.lookup(token) if token else self.resolve({}, {})
        if principal is None:
            self.failures[address] = [*recent, now]
            raise HTTPException(401, "bad UI token")
        self.sessions = {k: v for k, v in self.sessions.items() if v > now}
        if not self.identity.user(principal.user_id):
            self.identity.ensure_admin(secrets.token_urlsafe(32))
        issued = self.identity.issue(principal.user_id, name="Web 会话", ttl=3600,
                                     scopes=principal.scopes, parent=principal.token_id, kind="web")
        session = issued["token"]
        self.sessions[session] = now + 3600
        response.set_cookie("sleight_session", session, max_age=3600,
                            path=(request.scope.get("root_path", "") or "") + "/",
                            httponly=True, samesite="strict", secure=request.url.scheme == "https")
        response.headers["Cache-Control"] = "no-store"
        return {"ok": True, "expires_in": 3600}
