"""Host-local Chromium profiles, isolated user dirs and read-only page previews."""

from __future__ import annotations

import contextlib
import os
import platform
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import psutil

from ..core.errors import Busy, InstanceError, NotFound
from ..core.transport import Transport
from ..deploy.inventory import sleight_home
from ..providers.local import LocalLauncher
from ..service.database import storage_write
from .capabilities import browser_capabilities
from .governor import SQLiteLedger
from .inspector import inspect_browser


def platform_info() -> dict[str, Any]:
    system, arch = platform.system(), platform.machine().lower()
    return {"os": system, "arch": arch, "fingerprint_supported": arch in ("amd64", "x86_64"),
            "deployments_dir": str(sleight_home() / "deployments").replace("\\", "/"),
            "docker_target": "linux", "memory_bytes": psutil.virtual_memory().total}


def discover_browsers() -> list[dict[str, str]]:
    from ..browsers import installed

    rows = [{"name": p["name"], "binary": p["path"], **browser_capabilities(p["path"], p["name"])}
            for p in installed() if p.get("path")]
    candidates = {
        "Chromium": ["chromium", "chromium-browser"],
        "Chrome": ["google-chrome", "google-chrome-stable"],
        "Edge": ["microsoft-edge", "microsoft-edge-stable"],
    }
    if platform.system() == "Windows":
        for root in (os.environ.get("PROGRAMFILES", ""), os.environ.get("PROGRAMFILES(X86)", ""),
                     os.environ.get("LOCALAPPDATA", "")):
            candidates["Chrome"].append(str(Path(root) / "Google/Chrome/Application/chrome.exe"))
            candidates["Edge"].append(str(Path(root) / "Microsoft/Edge/Application/msedge.exe"))
    elif platform.system() == "Darwin":
        candidates["Chrome"].append("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
        candidates["Edge"].append("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge")
        candidates["Chromium"].append("/Applications/Chromium.app/Contents/MacOS/Chromium")
    for name, paths in candidates.items():
        for candidate in paths:
            binary = shutil.which(candidate) or (candidate if Path(candidate).is_file() else None)
            if binary:
                rows.append({"name": name, "binary": binary, **browser_capabilities(binary, name)})
                break
    return rows


def _process(row: dict) -> psutil.Process | None:
    if row.get("node") and row["node"] != platform.node():
        return None
    if not row.get("pid"):
        return None
    try:
        process = psutil.Process(row.get("pid", 0))
        return process if abs(process.create_time() - row.get("born", 0)) < .01 else None
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return None


class NativeSupervisor:
    def __init__(self, *, limit=3, ledger=None):
        self.limit = limit
        self.ledger = ledger or SQLiteLedger()
        self._launchers: dict[str, LocalLauncher] = {}
        self._preview_lock = threading.Lock()
        self._last_preview: dict[str, float] = {}

    def list(self) -> list[dict]:
        with self.ledger.transaction("native") as state:
            rows = [r for r in state["records"].values() if r.get("node", platform.node()) == platform.node()]
        for row in rows:
            process = _process(row)
            row["status"] = "running" if process else "stopped"
            row["memory_bytes"] = 0
            if process:
                with contextlib.suppress(psutil.NoSuchProcess, psutil.AccessDenied):
                    row["memory_bytes"] = sum(p.memory_info().rss for p in [process, *process.children(recursive=True)])
        return rows

    def get(self, pid: str) -> dict:
        row = next((r for r in self.list() if r["id"] == pid), None)
        if row is None:
            raise NotFound("No such native profile")
        return row

    @storage_write
    def create(self, body: dict) -> dict:
        from ..service.database import database
        if database().storage_pending:
            raise Busy("数据库正在切换，请重启 Sleight 后继续操作")
        binary = str(body.get("binary", "")).strip()
        from ..providers.local import _resolve_binary

        binary = _resolve_binary(binary)
        name = str(body.get("name", "")).strip()
        if not name or len(name) > 120:
            raise ValueError("请输入 1–120 字符的实例名")
        plugins = [str(Path(p).resolve()) for p in body.get("extension_paths", [])]
        for plugin in plugins:
            if not (Path(plugin) / "manifest.json").is_file():
                raise ValueError("插件需要包含 manifest.json 的解包目录")
        capabilities = browser_capabilities(binary)
        if plugins and capabilities["extensions"] != "automatic":
            raise ValueError("此内核未确认支持自动加载插件；可在实例窗口中手动安装，或选择 fingerprint-chromium")
        seed = body.get("fingerprint") or body.get("fingerprint_seed") or None
        if seed and not capabilities["fingerprint"]:
            raise ValueError("此内核不支持指纹种子")
        proxy = str(body.get("proxy", ""))
        if proxy:
            parsed = urlsplit(proxy)
            if parsed.scheme not in ("http", "https", "socks5") or not parsed.hostname or not parsed.port:
                raise ValueError("代理格式需要 http(s)://host:port 或 socks5://host:port")
            if parsed.username or parsed.password:
                raise ValueError("原生 Chromium 不接受命令行代理认证；请使用本机转发代理或 Cloak Manager")
        pid = uuid.uuid4().hex
        row = {"id": pid, "name": name, "binary": binary, "headless": bool(body.get("headless", True)),
               "node": platform.node(),
               "extension_paths": plugins, "proxy": proxy, "no_sandbox": bool(body.get("no_sandbox", False)),
               "fingerprint": seed, "pid": 0, "born": 0, "port": 0, "kind": capabilities["kind"],
               "notes": str(body.get("notes", ""))[:1000],
               "profile_dir": str(sleight_home() / "native" / pid)}
        with self.ledger.transaction("native") as state:
            if any(r["name"].casefold() == name.casefold() for r in state["records"].values() if r.get("node", platform.node()) == platform.node()):
                raise Busy("同一执行环境中已有这个实例名称")
            state["records"][pid] = row
        return row

    @storage_write
    def update(self, pid: str, changes: dict) -> dict:
        allowed = {"name", "notes", "headless", "no_sandbox", "fingerprint_seed", "proxy", "extension_paths"}
        if set(changes) - allowed:
            raise ValueError("内核、数据目录和实例身份不能修改")
        with self.ledger.transaction("native") as state:
            old = state["records"].get(pid)
            if not old or old.get("node", platform.node()) != platform.node():
                raise NotFound("No such native profile")
            if _process(old):
                raise Busy("请先停止实例再修改配置")
            row = {**old, **{k: v for k, v in changes.items() if k != "fingerprint_seed"}}
            name = str(row["name"]).strip()
            if not name or len(name) > 100:
                raise ValueError("请输入 1–100 字符的实例名称")
            if any(r["id"] != pid and r["name"].strip().casefold() == name.casefold()
                   for r in state["records"].values() if r.get("node", platform.node()) == platform.node()):
                raise Busy("同一执行环境中已有这个实例名称")
            caps = browser_capabilities(old["binary"])
            if "fingerprint_seed" in changes:
                seed = changes["fingerprint_seed"]
                if not caps["fingerprint"] or type(seed) is not int or not 1 <= seed <= 2**31 - 1:
                    raise ValueError("此内核不支持该指纹种子")
                row["fingerprint"] = seed
            if "extension_paths" in changes:
                if not isinstance(changes["extension_paths"], list):
                    raise ValueError("插件目录需要列表")
                plugins = [str(Path(p).resolve()) for p in changes["extension_paths"]]
                if plugins and caps["extensions"] != "automatic":
                    raise ValueError("此内核不支持自动加载插件，请在实例窗口中手动安装")
                if any(not (Path(p) / "manifest.json").is_file() for p in plugins):
                    raise ValueError("插件需要包含 manifest.json 的解包目录")
                row["extension_paths"] = plugins
            if "proxy" in changes and row["proxy"]:
                parsed = urlsplit(str(row["proxy"]))
                if parsed.scheme not in ("http", "https", "socks5") or not parsed.hostname or not parsed.port:
                    raise ValueError("代理格式需要 http(s)://host:port 或 socks5://host:port")
                if parsed.username or parsed.password:
                    raise ValueError("原生 Chromium 需使用无认证本机转发代理")
            for key in ("headless", "no_sandbox"):
                if key in changes and type(changes[key]) is not bool:
                    raise ValueError("启动选项需要布尔值")
            row.update(name=name, notes=str(row.get("notes") or "")[:1000], pid=0, born=0, port=0)
            state["records"][pid] = row
        return self.get(pid)

    @storage_write
    def start(self, pid: str) -> dict:
        from ..service.database import database
        if database().storage_pending:
            raise Busy("数据库正在切换，请重启 Sleight 后继续操作")
        # SQLite reservation is held through start: serializes host-local browser launches.
        with self.ledger.transaction("native") as state:
            records = state["records"]
            row = records.get(pid)
            if not row:
                raise NotFound("No such native profile")
            if row.get("node", platform.node()) != platform.node():
                raise NotFound("Native profile belongs to another execution host")
            if _process(row):
                return row
            if sum(bool(_process(r)) for r in records.values()) >= self.limit:
                raise Busy(f"本机浏览器容量 {self.limit} 已满，请先停止闲置实例")
            args = ["--no-first-run", "--no-default-browser-check"]
            if row["proxy"]:
                args.append("--proxy-server=" + row["proxy"])
            if row["extension_paths"]:
                paths = ",".join(row["extension_paths"])
                args.extend(["--load-extension=" + paths, "--disable-extensions-except=" + paths])
            Path(row["profile_dir"]).mkdir(parents=True, exist_ok=True)
            launcher = LocalLauncher(row["binary"], headless=row["headless"], no_sandbox=row["no_sandbox"],
                                     fingerprint=row["fingerprint"], profile_dir=row["profile_dir"], args=args)
            try:
                launcher.ensure_ready("default")
            except BaseException:
                launcher.close()
                raise
            process = psutil.Process(launcher._proc.pid)
            row.update(pid=process.pid, born=process.create_time(), port=launcher._port)
            self._launchers[pid] = launcher
        return self.get(pid)

    def stop(self, pid: str) -> None:
        row = self.get(pid)
        process = _process(row)
        if process:
            children = process.children(recursive=True)
            process.terminate()
            _, alive = psutil.wait_procs([process, *children], timeout=5)
            for child in alive:
                with contextlib.suppress(psutil.NoSuchProcess):
                    child.kill()
        launcher = self._launchers.pop(pid, None)
        if launcher:
            launcher.close()
        with self.ledger.transaction("native") as state:
            state["records"][pid].update(pid=0, born=0, port=0)

    def delete(self, pid: str, confirm: str) -> None:
        row = self.get(pid)
        if confirm != row["name"]:
            raise ValueError("删除会清空登录态，请输入完整实例名确认")
        self.stop(pid)
        path = Path(row["profile_dir"]).resolve()
        root = (sleight_home() / "native").resolve()
        if path.parent != root or path.name != pid:
            raise ValueError("Unsafe profile directory")
        if path.exists():
            shutil.rmtree(path)
        with self.ledger.transaction("native") as state:
            del state["records"][pid]

    def preview(self, pid: str, target: str = "") -> dict:
        if not self._preview_lock.acquire(blocking=False):
            raise Busy("预览忙，请稍后刷新")
        try:
            now = time.monotonic()
            key = pid + ":" + target
            if now - self._last_preview.get(key, 0) < 1:
                raise Busy("预览最多每秒一帧")
            self._last_preview[key] = now
            if len(self._last_preview) > 256:
                self._last_preview = {k: v for k, v in self._last_preview.items() if now - v < 30}
            row = self.get(pid)
            if not _process(row):
                raise InstanceError("实例未运行")
            from ..providers.plain import Plain

            endpoint = Plain(f"http://127.0.0.1:{row['port']}").endpoint()
            with Transport.connect(endpoint.ws_url) as transport:
                return inspect_browser(transport, target=target)
        finally:
            self._preview_lock.release()

    def close(self) -> None:
        for pid in list(self._launchers):
            self.stop(pid)
