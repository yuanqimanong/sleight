"""Background resource sampling and sustained, deduplicated Lark alerts."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import threading
import time
from urllib.parse import urlsplit

import httpx
import psutil

from ..deploy.store import Store
from .database import database
from .identity import Vault

DEFAULTS = {"cpu_percent": 85, "memory_percent": 80, "duration_s": 60, "cooldown_s": 900, "target": "host"}


class Monitor:
    def __init__(self, *, clock=time.time, sender=None):
        self.db, self.vault = database(), Vault()
        self.clock, self.sender = clock, sender or self.send
        self.snapshot = {"host": {}, "containers": [], "sampled_at": 0}
        self._lock = threading.Lock()

    def settings(self):
        row = {**DEFAULTS, **self.db.get("settings", "alerts", {})}
        row["configured"] = bool(self.vault.get("alert-webhook"))
        row["signing_configured"] = bool(self.vault.get("alert-signing"))
        return row

    def save(self, body):
        row = {**DEFAULTS, **self.db.get("settings", "alerts", {})}
        for key in ("cpu_percent", "memory_percent", "duration_s", "cooldown_s"):
            if key in body:
                row[key] = float(body[key])
        if not all(1 <= row[k] <= 100 for k in ("cpu_percent", "memory_percent")) or not 5 <= row["duration_s"] <= 86400 or not 60 <= row["cooldown_s"] <= 86400:
            raise ValueError("阈值需为 1–100%，持续时间 5–86400 秒，通知间隔 60–86400 秒")
        target = str(body.get("target", row["target"]))
        if target != "host" and target not in {d.ref for d in Store().deployments()}:
            raise ValueError("请选择有效监控主机或 Manager 环境")
        row["target"] = target
        if "webhook" in body:
            webhook = str(body["webhook"]).strip()
            url = urlsplit(webhook)
            if webhook and (url.scheme != "https" or url.hostname not in ("open.larksuite.com", "open.feishu.cn") or not url.path.startswith("/open-apis/bot/v2/hook/") or url.username or url.password):
                raise ValueError("请填写 Lark / 飞书机器人的 HTTPS Webhook 地址")
            self.vault.put("alert-webhook", webhook)
        if "signing_secret" in body:
            self.vault.put("alert-signing", str(body["signing_secret"]).strip())
        self.db.put("settings", "alerts", row)
        self.db.delete("settings", "alert-state")
        return self.settings()

    def sample(self):
        memory = psutil.virtual_memory()
        host = {"name": "Sleight 执行主机", "cpu_percent": psutil.cpu_percent(interval=None), "memory_percent": memory.percent,
                "memory_used": memory.total - memory.available, "memory_total": memory.total, "kind": "host"}
        containers = []
        for deployment in Store().deployments():
            try:
                saved = Store().require_host(deployment.host)
                with saved.runner() as runner:
                    result = runner.run(["docker", "stats", "--no-stream", "--format", "{{json .}}", deployment.spec.container_name], timeout=8, sudo=saved.sudo)
                    if result.ok and result.text:
                        data = json.loads(result.text.splitlines()[0])
                        used, total = data["MemUsage"].split("/")
                        raw_cpu = float(data["CPUPerc"].rstrip("%"))
                        quota = float(deployment.spec.cpus)
                        containers.append({"id": deployment.ref, "name": deployment.ref, "kind": "container", "cpu_percent": raw_cpu / quota,
                                           "cpu_raw_percent": raw_cpu, "cpu_limit": quota, "scope_note": "CPU 相对于配置的容器 CPU 上限；内存相对于容器限额",
                                           "memory_percent": float(data["MemPerc"].rstrip("%")), "memory_used": size_bytes(used), "memory_total": size_bytes(total)})
            except Exception:
                containers.append({"id": deployment.ref, "name": deployment.ref, "kind": "container", "unavailable": True})
        value = {"host": host, "containers": containers, "sampled_at": self.clock()}
        with self._lock:
            self.snapshot = value
        self.evaluate(value)
        return value

    def metrics(self):
        with self._lock:
            return dict(self.snapshot)

    def evaluate(self, metrics):
        config = self.settings()
        if not config["configured"]:
            return
        row = metrics["host"] if config["target"] == "host" else next((r for r in metrics["containers"] if r["id"] == config["target"]), None)
        now = self.clock()
        state = self.db.get("settings", "alert-state", {"since": None, "last_sent": 0, "active": False})
        if not row or row.get("unavailable") or "cpu_percent" not in row:
            state["since"] = None
            self.db.put("settings", "alert-state", state)
            return
        if now - state.get("sampled_at", now) > 20:
            state["since"] = None
        state["sampled_at"] = now
        exceeded = row["cpu_percent"] >= config["cpu_percent"] or row["memory_percent"] >= config["memory_percent"]
        message = ""
        if exceeded:
            if state["since"] is None:
                state["since"] = now
            if now - state["since"] >= config["duration_s"] and (not state["active"] or now - state["last_sent"] >= config["cooldown_s"]):
                message = f"Sleight 资源告警 · {row['name']}\nCPU {row['cpu_percent']:.1f}% / 内存 {row['memory_percent']:.1f}%\n持续至少 {config['duration_s']:g} 秒，请检查运行实例。"
        else:
            state["since"] = None
            if state["active"]:
                message = f"Sleight 资源恢复 · {row['name']}\nCPU {row['cpu_percent']:.1f}% / 内存 {row['memory_percent']:.1f}%"
        if message and now >= state.get("retry_at", 0):
            try:
                self.sender(message)
                state.update(last_sent=now, active=exceeded, error="", retry_at=0, failures=0)
            except Exception:
                state["error"] = "通知发送失败，稍后重试；请检查 Webhook 和签名设置"
                state["failures"] = min(state.get("failures", 0) + 1, 6)
                state["retry_at"] = now + min(30 * 2 ** (state["failures"] - 1), 900)
        self.db.put("settings", "alert-state", state)

    def send(self, message):
        payload = {"msg_type": "text", "content": {"text": message}}
        secret = self.vault.get("alert-signing")
        if secret:
            timestamp = str(int(self.clock()))
            sign = base64.b64encode(hmac.new((timestamp + "\n" + secret).encode(), b"", hashlib.sha256).digest()).decode()
            payload.update(timestamp=timestamp, sign=sign)
        with httpx.Client(timeout=8, follow_redirects=False) as client:
            response = client.post(self.vault.get("alert-webhook"), json=payload)
            response.raise_for_status()
            if response.json().get("code", response.json().get("StatusCode", 0)) != 0:
                raise ValueError("机器人拒绝通知")


def size_bytes(value):
    import re
    match = re.fullmatch(r"\s*([\d.]+)\s*(B|KiB|MiB|GiB|TiB|kB|MB|GB|TB)\s*", value)
    if not match:
        raise ValueError("未知内存单位")
    powers = {"B": 1, "KiB": 1024, "MiB": 1024**2, "GiB": 1024**3, "TiB": 1024**4, "kB": 1000, "MB": 1000**2, "GB": 1000**3, "TB": 1000**4}
    return round(float(match[1]) * powers[match[2]])
