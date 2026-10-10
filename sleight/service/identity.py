"""User-owned, revocable credentials and encrypted service secrets."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import uuid
from contextvars import ContextVar
from dataclasses import dataclass

from cryptography.fernet import Fernet

from .database import database


@dataclass(frozen=True)
class Principal:
    user_id: str
    role: str
    token_id: str = ""
    scopes: tuple[str, ...] = ("execute",)


current: ContextVar[Principal | None] = ContextVar("sleight_principal", default=None)


def encryption_key(root):
    path = root / "secret.key"
    key = os.environ.get("SLEIGHT_SECRET_KEY")
    if not key:
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not path.exists():
            try:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                pass
            else:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(Fernet.generate_key())
        key = path.read_bytes()
    return key


class Vault:
    def __init__(self):
        self.db = database()
        self.cipher = Fernet(encryption_key(self.db.root))

    def get(self, name: str, default=""):
        value = self.db.get("secrets", name)
        return self.cipher.decrypt(value.encode()).decode() if value else default

    def put(self, name: str, value: str):
        self.db.put("secrets", name, self.cipher.encrypt(value.encode()).decode())

    def delete(self, name: str):
        self.db.delete("secrets", name)

    def import_file(self, filename: str, prefix: str):
        path = self.db.root / filename
        if self.db.get("migration", filename) or not path.is_file():
            return
        for key, value in json.loads(path.read_text(encoding="utf-8")).items():
            self.put(prefix + key, str(value))
        self.db.put("migration", filename, True)


class Identity:
    def __init__(self):
        self.db = database()

    def ensure_admin(self, token: str):
        with self.db.state("identity") as state:
            users, tokens = state.setdefault("users", {}), state.setdefault("tokens", {})
            users.setdefault("admin", {"id": "admin", "name": "管理员", "role": "admin", "active": True,
                                       "default_environment": "native", "default_template": "", "limit": 3})
            digest = self.digest(token)
            if digest not in tokens:
                tokens[digest] = {"id": uuid.uuid4().hex, "user_id": "admin", "name": "启动凭据",
                                  "scopes": ["execute", "manage", "delegate"], "expires": 0,
                                  "revoked": False, "parent": "", "created": self.db.now()}

    @staticmethod
    def digest(token: str):
        return hashlib.sha256(token.encode()).hexdigest()

    def lookup(self, token: str) -> Principal | None:
        if not token:
            return None
        with self.db.state("identity") as state:
            records = state.get("tokens", {})
            row = records.get(self.digest(token))
            if not row:
                return None
            seen = set()
            candidate = row
            while candidate:
                if candidate["id"] in seen or candidate.get("revoked") or (candidate.get("expires") and candidate["expires"] <= self.db.now()):
                    return None
                if not state.get("users", {}).get(candidate["user_id"], {}).get("active"):
                    return None
                seen.add(candidate["id"])
                parent = candidate.get("parent")
                candidate = next((v for v in records.values() if v["id"] == parent), None) if parent else None
                if parent and candidate is None:
                    return None
            user = state.get("users", {}).get(row["user_id"])
            if not user or not user.get("active"):
                return None
            return Principal(user["id"], user["role"], row["id"], tuple(row["scopes"]))

    def valid_id(self, token_id: str) -> bool:
        with self.db.state("identity") as state:
            records = {r["id"]: r for r in state.get("tokens", {}).values()}
            row = records.get(token_id)
            seen = set()
            while row:
                if row["id"] in seen or row.get("revoked") or (row.get("expires") and row["expires"] <= self.db.now()):
                    return False
                if not state.get("users", {}).get(row["user_id"], {}).get("active"):
                    return False
                seen.add(row["id"])
                if not row.get("parent"):
                    return True
                row = records.get(row["parent"])
            return False

    def users(self):
        with self.db.state("identity") as state:
            return list(state.get("users", {}).values())

    def user(self, user_id: str):
        return next((r for r in self.users() if r["id"] == user_id), None)

    def save_user(self, body: dict):
        name = str(body.get("name", "")).strip()
        if not name or len(name) > 120 or body.get("role", "operator") not in ("admin", "operator"):
            raise ValueError("用户名称或角色无效")
        with self.db.state("identity") as state:
            users = state.setdefault("users", {})
            uid = body.get("id") or uuid.uuid4().hex
            old = users.get(uid, {})
            row = {"id": uid, "name": name, "role": body.get("role", old.get("role", "operator")),
                   "active": bool(body.get("active", old.get("active", True))),
                   "default_environment": str(body.get("default_environment", old.get("default_environment", "native"))),
                   "default_launch_template": str(body.get("default_launch_template", old.get("default_launch_template", ""))),
                   "default_template": str(body.get("default_template", old.get("default_template", ""))),
                   "limit": int(body.get("limit", old.get("limit", 3)))}
            for key in ("environments", "profile_names"):
                values = body.get(key, old.get(key, []))
                if not isinstance(values, list) or len(values) > 100 or not all(isinstance(v, str) and len(v) <= 200 for v in values):
                    raise ValueError("授权环境或实例列表无效")
                row[key] = values
            if not 1 <= row["limit"] <= 100:
                raise ValueError("用户并发上限必须为 1–100")
            users[uid] = row
            return row

    def issuance_parent(self, token_id: str):
        """Durable credentials survive a Web login, but retain the granting token chain."""
        with self.db.state("identity") as state:
            records = {r["id"]: r for r in state.get("tokens", {}).values()}
            seen = set()
            row = records.get(token_id)
            while row and row.get("kind") in ("web", "delegation") and row.get("parent"):
                if row["id"] in seen:
                    raise PermissionError("凭据父链无效")
                seen.add(row["id"])
                row = records.get(row["parent"])
            return row["id"] if row else token_id

    def issue(self, user_id: str, *, name="API", ttl=0, scopes=None, parent="", kind="api"):
        if ttl < 0 or set(scopes or ["execute"]) - {"execute", "manage", "delegate"}:
            raise ValueError("凭据有效期或权限范围无效")
        if not self.user(user_id):
            raise ValueError("用户不存在")
        raw = "sl_" + secrets.token_urlsafe(32)
        now = self.db.now()
        row = {"id": uuid.uuid4().hex, "user_id": user_id, "name": str(name)[:120],
               "scopes": list(scopes or ["execute"]), "expires": self.db.now() + ttl if ttl else 0,
               "revoked": False, "parent": parent, "created": self.db.now(), "kind": kind}
        with self.db.state("identity") as state:
            if sum(r["user_id"] == user_id and not r.get("revoked") and (not r.get("expires") or r["expires"] > now)
                   for r in state.get("tokens", {}).values()) >= 200:
                raise ValueError("此用户有效 token 已达 200 个，请先撤销不再使用的凭据")
            state.setdefault("tokens", {})[self.digest(raw)] = row
        return {**row, "token": raw}

    def tokens(self, user_id=None):
        with self.db.state("identity") as state:
            return [r for r in state.get("tokens", {}).values() if user_id is None or r["user_id"] == user_id]

    def revoke(self, token_id: str):
        with self.db.state("identity") as state:
            row = next((r for r in state.get("tokens", {}).values() if r["id"] == token_id), None)
            if not row:
                raise ValueError("凭据不存在")
            row["revoked"] = True

    def prune(self):
        now = self.db.now()
        with self.db.state("identity") as state:
            tokens = state.get("tokens", {})
            for key, row in list(tokens.items()):
                if row.get("expires") and row["expires"] < now - 86400:
                    del tokens[key]

    def delegate(self, principal: Principal, external_id: str, name: str, ttl: int):
        if "delegate" not in principal.scopes or principal.role != "admin":
            raise PermissionError("接入凭据没有身份委托权限")
        key = principal.user_id + ":" + external_id
        with self.db.state("identity") as state:
            external = state.setdefault("external", {})
            uid = external.get(key)
            if not uid:
                uid = uuid.uuid4().hex
                state.setdefault("users", {})[uid] = {"id": uid, "name": name[:120], "role": "admin", "active": True,
                    "default_environment": "native", "default_template": "", "limit": 3}
                external[key] = uid
        return self.issue(uid, name="委托会话", ttl=min(max(ttl, 30), 3600),
                          scopes=["execute", "manage"], parent=principal.token_id, kind="delegation")
