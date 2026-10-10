"""Shared launch templates. Secrets stay in a private file, never API responses."""

import uuid
from urllib.parse import urlsplit, urlunsplit

from ..service.identity import Identity, Vault, current
from .governor import SQLiteLedger


def mask_proxy(proxy: str) -> str:
    if not proxy:
        return ""
    parsed = urlsplit(proxy)
    if not parsed.hostname:
        return "***"
    host = parsed.hostname
    if ":" in host:
        host = "[" + host + "]"
    netloc = ("***@" if parsed.username else "") + host + (f":{parsed.port}" if parsed.port else "")
    return urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))


class Templates:
    def __init__(self):
        self.ledger = SQLiteLedger()
        self.vault = Vault()
        self.vault.import_file("config-secrets.json", "template:")

    def list(self):
        user = current.get()
        assigned = (Identity().user(user.user_id) or {}).get("default_template") if user else ""
        with self.ledger.transaction("templates") as state:
            return [{k: v for k, v in r.items() if k != "proxy_secret"} for r in state["records"].values()
                    if not user or (user.role == "admin" and "manage" in user.scopes) or r.get("owner", "admin") == user.user_id or r.get("shared") or r["id"] == assigned]

    def save(self, body):
        pid = body.get("id") or uuid.uuid4().hex
        name = str(body.get("name", "")).strip()
        if not name:
            raise ValueError("请输入模板名")
        legacy = self.vault.get("template:" + pid)
        with self.ledger.transaction("templates") as state:
            proxy = str(body.get("proxy", ""))
            user = current.get()
            old = state["records"].get(pid, {})
            if old and user and not (user.role == "admin" and "manage" in user.scopes) and old.get("owner", "admin") != user.user_id:
                raise ValueError("不能修改其他用户的模板")
            if "proxy" in body:
                if "\n" in proxy or "\r" in proxy:
                    raise ValueError("代理不能包含换行")
                secret = self.vault.cipher.encrypt(proxy.encode()).decode()
            else:
                secret = old.get("proxy_secret", "")
                proxy = self.vault.cipher.decrypt(secret.encode()).decode() if secret else legacy
                if proxy and not secret:
                    secret = self.vault.cipher.encrypt(proxy.encode()).decode()
            if user and not (user.role == "admin" and "manage" in user.scopes) and body.get("shared"):
                raise PermissionError("只有管理员可以共享模板")
            row = {"id": pid, "name": name, "extension_paths": body.get("extension_paths", []),
                   "proxy_display": mask_proxy(proxy), "proxy_secret": secret,
                   "owner": old.get("owner", user.user_id if user else "admin"),
                   "shared": bool(body.get("shared", old.get("shared", False)))}
            state["records"][pid] = row
            return {k: v for k, v in row.items() if k != "proxy_secret"}

    def apply(self, body):
        pid = body.get("template_id")
        if not pid:
            return body
        user = current.get()
        assigned = (Identity().user(user.user_id) or {}).get("default_template") if user else ""
        with self.ledger.transaction("templates") as state:
            row = state["records"].get(pid)
            if not row:
                raise ValueError("模板已删除，请重新选择")
            if user and not (user.role == "admin" and "manage" in user.scopes) and row.get("owner", "admin") != user.user_id and not row.get("shared") and pid != assigned:
                raise ValueError("无权使用此模板")
        secret = row.get("proxy_secret")
        proxy = self.vault.cipher.decrypt(secret.encode()).decode() if secret else self.vault.get("template:" + pid)
        # Per-task fields (seed, SID, proxy override) always win.
        return {"extension_paths": row["extension_paths"], "proxy": proxy, **body}

    def delete(self, pid):
        with self.ledger.transaction("templates") as state:
            if pid not in state["records"]:
                raise ValueError("模板不存在")
            user = current.get()
            if user and not (user.role == "admin" and "manage" in user.scopes) and state["records"][pid].get("owner", "admin") != user.user_id:
                raise ValueError("不能删除其他用户的模板")
            del state["records"][pid]
        self.vault.delete("template:" + pid)


def install_browser(name: str, say):
    from ..browsers import install

    if name == "fingerprint-chromium":
        last = [-1]
        def progress(done, total):
            percent = int(done * 100 / total) if total else done // (10 * 1024 * 1024)
            bucket = percent // 10 if total else percent
            if bucket != last[0]:
                last[0] = bucket
                say(f"下载进度 {percent}%" if total else f"已下载 {done // (1024 * 1024)} MB")
        return {"binary": str(install(name, on_progress=progress))}
    raise ValueError("可下载 fingerprint-chromium；其他原生浏览器请选择已有可执行文件")
