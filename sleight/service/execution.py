"""Browser sessions execute on one owning thread, with bounded RPC queues."""

from __future__ import annotations

import base64
import dataclasses
import ipaddress
import platform
import queue
import threading
import uuid
from concurrent.futures import Future
from contextlib import ExitStack, contextmanager, suppress
from enum import Enum

from ..core import errors
from ..core.session import Session
from ..core.transport import Transport
from ..deploy.engine import Deployer
from ..deploy.store import Store
from ..lease.database import DatabaseLease
from ..providers.cloakbrowser import ProfileSpec, Region
from ..providers.plain import Plain
from ..runtime.config import Templates, mask_proxy
from ..runtime.native import NativeSupervisor
from .database import database, storage_write
from .identity import Identity, Principal, current

SESSION_METHODS = frozenset({"open", "fetch", "reload", "back", "forward", "url", "title", "eval", "call", "query", "query_all",
    "click", "type", "press", "scroll", "hover", "wait", "pump_events", "cookies",
    "clear_site_data", "exit_ip", "parse", "snapshot", "screenshot",
    "capture_resources", "block", "text", "content", "outer_html", "extract_document", "clear_browser_data",
    "history", "viewport", "query_shadow", "require", "double_click", "scroll_into_view", "select_option"})
OBJECT_METHODS = frozenset({"snapshot", "urls", "by_type", "reset", "query", "query_all", "xpath", "attr", "text", "html",
    "inner_text", "exists", "box", "require_box", "value", "in_viewport", "scroll_metrics", "screenshot", "select_option"})
CDP_METHODS = frozenset({"Browser.getVersion", "Target.getTargets", "Network.getResponseBody", "Network.getCookies",
    "Storage.getCookies", "Page.captureScreenshot", "Runtime.evaluate"})


@contextmanager
def manager_for(environment: str):
    store = Store()
    entry = store.resolve(environment)
    host = store.require_host(entry.host)
    with host.runner() as runner, Deployer(entry.spec, runner, sudo=host.sudo).connect() as manager:
        yield manager


class Controller:
    def __init__(self):
        self.db, self.identity = database(), Identity()

    def profile(self, pid: str, user: Principal):
        row = self.db.get("profiles", pid)
        if not row or (row["owner"] != user.user_id and not (user.role == "admin" and "manage" in user.scopes)):
            raise errors.NotFound("实例不存在或没有权限")
        if row["environment"] == "native" and row.get("node", platform.node()) != platform.node():
            raise errors.NotFound("本机实例属于其他执行主机")
        return row

    @storage_write
    def create(self, body: dict, user: Principal, request_id: str):
        if self.db.storage_pending:
            raise errors.Busy("数据库正在切换，请重启 Sleight 后继续操作")
        if not request_id or len(request_id) > 120:
            raise ValueError("创建实例需要 Idempotency-Key")
        default = self.identity.user(user.user_id) or {}
        environment = str(body.get("environment") or default.get("default_environment") or "native")
        allowed = default.get("environments", [])
        if not (user.role == "admin" and "manage" in user.scopes) and environment != default.get("default_environment") and environment not in allowed:
            raise PermissionError("此用户未获授权使用目标环境")
        template_id = body.get("template_id") or default.get("default_template")
        source = {k: v for k, v in body.items() if k in {"name", "proxy", "fingerprint_seed", "region", "headless", "binary", "ephemeral", "profile_name", "sid"}}
        if not (user.role == "admin" and "manage" in user.scopes):
            source.pop("binary", None)
            if environment == "native" and body.get("kernel_id"):
                import hashlib

                from ..runtime.native import discover_browsers
                candidate = next((r for r in discover_browsers() if hashlib.sha256(r["binary"].encode()).hexdigest()[:16] == body["kernel_id"]), None)
                if not candidate:
                    raise ValueError("所选内核不存在，请重新选择")
                source["binary"] = candidate["binary"]
        source["template_id"] = template_id
        source = Templates().apply(source)
        from ..runtime.capabilities import browser_capabilities
        from .launch_templates import FIELDS, LaunchTemplates
        launch_id = body.get("launch_template_id") or default.get("default_launch_template")
        launch = LaunchTemplates().get(launch_id) if launch_id else None
        if launch:
            source = {**launch["config"], **source}
        for key in FIELDS | {"kind", "no_sandbox"}:
            if key in body:
                source[key] = body[key]
        if user.role == "admin" and "manage" in user.scopes and "extension_paths" in body:
            source["extension_paths"] = body["extension_paths"]
        if bool(source.get("ephemeral", True)) and "fingerprint_seed" not in body:
            source.pop("fingerprint_seed", None)
            if environment != "native":
                source["fingerprint_seed"] = "random"
        if source.get("proxy") and "{sid}" in source["proxy"]:
            source["sid"] = str(source.get("sid") or uuid.uuid4().hex[:10])
            source["proxy"] = source["proxy"].replace("{sid}", source["sid"])
        request_key = user.user_id + ":" + request_id
        with self.db.state("profile-requests") as state:
            previous = state["records"].get(request_key)
            if previous:
                if previous["state"] == "ready":
                    return self.profile(previous["id"], user)
                raise errors.Busy("实例创建状态待确认，请使用原请求标识稍后查询")
            name = str(body.get("name") or "实例-" + uuid.uuid4().hex[:8]).strip()
            if not name or len(name) > 100:
                raise ValueError("请输入 1–100 字符的实例名称")
            for record in state["records"].values():
                if record.get("environment") == environment and record.get("name") == name.casefold() and (record["state"] == "creating" or self.db.get("profiles", record["id"])):
                    raise errors.Busy("同一执行环境中已有这个实例名称")
            if any(r["environment"] == environment and r["name"].strip().casefold() == name.casefold() for r in self.db.list("profiles")):
                raise errors.Busy("同一执行环境中已有这个实例名称")
            pid = uuid.uuid4().hex
            state["records"][request_key] = {"id": pid, "state": "creating", "time": self.db.now(), "environment": environment, "name": name.casefold()}
        row = {"id": pid, "owner": user.user_id, "environment": environment, "name": name,
               "ephemeral": bool(body.get("ephemeral", True)), "created": self.db.now(), "touched": self.db.now(),
               "state": "creating", "source_id": ""}
        row["fingerprint_seed"] = source.get("fingerprint_seed")
        row["template_id"] = template_id or ""
        row["launch_template_id"] = launch_id or ""
        row["notes"] = str(source.get("notes") or "")[:1000]
        row["proxy_display"] = mask_proxy(source.get("proxy", ""))
        row["proxy_sid"] = source.get("sid", "")
        if environment == "native":
            row["node"] = platform.node()
        self.db.put("profiles", pid, row)
        try:
            if environment == "native":
                if not source.get("binary"):
                    from ..runtime.native import discover_browsers
                    browsers = discover_browsers()
                    if not browsers:
                        raise errors.NotFound("未安装本机浏览器，请先在 Web 安装并配置默认环境")
                    kind = body.get("kind") or (launch["kind"] if launch else "native")
                    candidate = next((b for b in browsers if b["kind"] == kind), None)
                    if not candidate:
                        raise errors.NotFound("未安装所选内核，请先到部署环境安装")
                    source["binary"] = candidate["binary"]
                if browser_capabilities(source["binary"])["fingerprint"] and not source.get("fingerprint_seed"):
                    import secrets
                    source["fingerprint_seed"] = secrets.randbelow(2**31 - 1) + 1
                row["kind"] = browser_capabilities(source["binary"])["kind"]
                row["fingerprint_seed"] = source.get("fingerprint_seed")
                created = NativeSupervisor().create({**source, "name": row["name"]})
                row["source_id"] = created["id"]
            else:
                with manager_for(environment) as manager:
                    fixed = source.get("profile_name")
                    if fixed:
                        if not (user.role == "admin" and "manage" in user.scopes) and fixed not in default.get("profile_names", []):
                            raise PermissionError("未授权使用此固定实例")
                        existing = manager.find_profile(fixed)
                        if not existing:
                            raise errors.NotFound("固定实例不存在")
                        row.update(source_id=existing["id"], ephemeral=False, external=True,
                                   proxy_display=mask_proxy(existing.get("proxy") or ""), proxy_sid="")
                    else:
                        if any(p["name"].strip().casefold() == name.casefold() for p in manager.list_profiles()):
                            raise errors.Busy("Manager 中已有这个实例名称")
                        paths = ",".join(source.get("extension_paths", []))
                        args = (f"--disable-extensions-except={paths}", f"--load-extension={paths}") if paths else ()
                        region = Region(source["region"]) if source.get("region") else Region.US_EAST
                        config = {k: v for k, v in source.items() if k in FIELDS and v not in (None, "")}
                        if launch:
                            capabilities = manager.capabilities()
                            if capabilities["generation"] == "modern":
                                gpu = str(config.get("gpu_vendor", "")).lower()
                                config["gpu_family"] = config.get("gpu_family") or ("nvidia" if "nvidia" in gpu else "intel" if "intel" in gpu else "auto")
                                config.pop("gpu_vendor", None)
                                config.pop("gpu_renderer", None)
                        spec = ProfileSpec.windows(row["name"] + "-" + pid[:8], region, proxy=source.get("proxy") or None,
                            launch_args=args, extension_paths=tuple(source.get("extension_paths", [])),
                            tags=("ephemeral", "sleight-request:" + pid) if row["ephemeral"] else ("sleight-request:" + pid,), **config)
                        created = manager.create_profile(spec, launch=False)
                        row["source_id"] = created.id
            row["state"] = "idle"
            self.db.put("profiles", pid, row)
            with self.db.state("profile-requests") as state:
                state["records"][request_key]["state"] = "ready"
            return row
        except BaseException:
            row["state"] = "cleanup_pending"
            self.db.put("profiles", pid, row)
            with self.db.state("profile-requests") as state:
                state["records"][request_key]["state"] = "failed"
            raise

    def cleanup(self, row: dict, *, delete=False):
        if not row.get("source_id"):
            if row["environment"] != "native":
                with manager_for(row["environment"]) as manager:
                    found = [p for p in manager.list_profiles() if any(t.get("tag") == "sleight-request:" + row["id"] for t in p.get("tags", []))]
                    if len(found) > 1:
                        raise errors.InstanceError("创建响应丢失且匹配多个实例，请管理员核对")
                    if found:
                        row = {**row, "source_id": found[0]["id"]}
            if not row.get("source_id"):
                if delete:
                    self.db.delete("profiles", row["id"])
                return
        if row["environment"] == "native":
            if row.get("node", platform.node()) != platform.node():
                raise errors.InstanceError("其他执行主机的本机浏览器不能在此回收")
            native = NativeSupervisor()
            with suppress(errors.NotFound):
                profile = native.get(row["source_id"])
                native.delete(profile["id"], profile["name"]) if delete else native.stop(profile["id"])
        else:
            with manager_for(row["environment"]) as manager:
                try:
                    if delete and not row.get("external"):
                        manager.delete_profile(row["source_id"], force=True)
                    else:
                        manager.stop(row["source_id"])
                except errors.NotFound:
                    pass
        DatabaseLease().cleaned(row["source_id"], namespace="service:" + row["environment"])
        if delete:
            self.db.delete("profiles", row["id"])
        else:
            self.db.put("profiles", row["id"], {**row, "state": "idle", "touched": self.db.now()})


class Actor:
    def __init__(self, supervisor, row: dict, user: Principal, options: dict):
        self.supervisor, self.row, self.user, self.options = supervisor, row, user, options
        self.db = supervisor.db
        self.queue = queue.Queue(maxsize=64)
        self.stop = threading.Event()
        self.ready = Future()
        self.done = threading.Event()
        self.objects = {}
        self.scopes = {}
        self.transport = None
        self.lease = None
        self.owns = False
        self.thread = threading.Thread(target=self.run, daemon=True, name="sleight-session-" + row["id"][:8])
        self.thread.start()

    def submit(self, body: dict):
        if self.stop.is_set():
            raise errors.SessionLost("会话已关闭")
        future = Future()
        try:
            self.queue.put_nowait((body, future))
        except queue.Full as exc:
            raise errors.Busy("会话操作队列已满") from exc
        timeout = min(max(float(body.get("timeout", 60)), .1), 300)
        try:
            return future.result(timeout=timeout + 2)
        except TimeoutError as exc:
            self.close()
            raise errors.TimeoutError("远程操作超时，会话已取消，操作不会自动重放") from exc

    def encode(self, value):
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        if isinstance(value, Enum):
            return value.value
        if isinstance(value, bytes):
            return {"$bytes": base64.b64encode(value).decode()}
        if isinstance(value, (tuple, list, set, frozenset)):
            return [self.encode(v) for v in value]
        if isinstance(value, dict):
            return {k: self.encode(v) for k, v in value.items()}
        if dataclasses.is_dataclass(value) and type(value).__name__ != "ResourceTracker":
            return {"$value": type(value).__name__, "fields": self.encode(dataclasses.asdict(value))}
        key = next((k for k, v in self.objects.items() if v is value), None)
        if key is None:
            if len(self.objects) >= 2048:
                raise errors.Busy("会话对象上限已满，请结束会话后重新申请")
            key = uuid.uuid4().hex
            self.objects[key] = value
        return {"$object": key, "type": type(value).__name__}

    def decode(self, value):
        if isinstance(value, list):
            return [self.decode(v) for v in value]
        if isinstance(value, dict):
            if "$object" in value:
                return self.objects[value["$object"]]
            if "$condition" in value:
                from ..core import types
                cls = getattr(types, value["$condition"], None)
                if value["$condition"] not in ("DomReady", "Load", "NetworkIdle", "Text", "Selector", "Gone"):
                    raise ValueError("不支持此等待条件")
                return cls(**value.get("fields", {}))
            return {k: self.decode(v) for k, v in value.items()}
        return value

    def execute(self, session, body):
        method = body.get("method", "")
        if method == "$inspect":
            from ..runtime.inspector import inspect_browser
            return inspect_browser(session.transport, **body.get("kwargs", {}))
        if method == "$probe_exit_ip":
            options = body.get("kwargs", {})
            timeout = min(max(float(options.get("timeout", 15)), .1), 60)
            transport = session.transport
            cid = transport.call("Target.createBrowserContext", {"disposeOnDetach": True}, timeout=timeout)["browserContextId"]
            try:
                with Session.create(transport, browser_context_id=cid, human=False) as probe:
                    result = probe.eval(str(options.get("expression") or "null"))
                    if isinstance(result, dict):
                        with suppress(ValueError):
                            return {**result, "ip": str(ipaddress.ip_address(str(result.get("ip") or "").strip()))}
                    return {"ip": probe.exit_ip(endpoints=options.get("endpoints"), timeout=timeout)}
            finally:
                with suppress(errors.SleightError):
                    transport.call("Target.disposeBrowserContext", {"browserContextId": cid}, timeout=timeout)
        if method == "$mcp":
            from ..agent.gateway import Gateway
            from ..agent.mcp import MCPServer
            gateway = getattr(self, "gateway", None)
            if gateway is None:
                gateway = self.gateway = Gateway(session)
            return MCPServer(lambda: gateway).handle(body["message"])
        obj = self.objects.get(body.get("object")) if body.get("object") else session
        if body.get("object") and obj is None:
            raise errors.StaleRef("远程对象已失效")
        if method == "$exit":
            context = self.scopes.pop(body["object"], None)
            if context:
                context.__exit__(None, None, None)
            if type(obj).__name__ == "ResourceTracker":
                self.objects.pop(body["object"], None)
            return None
        if method == "$get":
            name = body.get("name", "")
            if name not in ("blocked", "by_type", "allowed", "closed", "html", "text", "url", "pending", "failed", "finished", "size", "target_id", "cdp_session_id", "children", "tag", "attrs"):
                raise PermissionError("不允许读取此属性")
            return self.encode(getattr(obj, name))
        if obj is session and method not in SESSION_METHODS and method != "$cdp":
            raise PermissionError("不允许此会话操作")
        if not method or method.startswith("_"):
            raise PermissionError("不允许访问内部方法")
        args, kwargs = self.decode(body.get("args", [])), self.decode(body.get("kwargs", {}))
        if method == "capture_resources":
            kwargs["max_resources"] = 2000
        if method == "screenshot" and (args or kwargs.get("path")):
            raise PermissionError("截图以字节返回，不能指定服务端文件路径")
        if obj is not session and method not in OBJECT_METHODS:
            raise PermissionError("不允许此对象操作")
        if method in ("$cdp", "call"):
            if args[0] not in CDP_METHODS:
                raise PermissionError("此 CDP 方法未授权")
            if args[0] in ("Runtime.evaluate", "Network.getResponseBody", "Network.getCookies", "Page.captureScreenshot"):
                kwargs.pop("session_id", None)
                result = session.call(*args, **kwargs)
            else:
                result = session.transport.call(*args, **kwargs)
            if args[0] == "Target.getTargets":
                target_id = getattr(session, "target_id", "")
                result["targetInfos"] = [r for r in result.get("targetInfos", [])
                    if r.get("targetId") == target_id or r.get("url", "").startswith("chrome-extension://")]
        else:
            result = getattr(obj, method)(*args, **kwargs)
        if obj is session and method in ("open", "reload", "back", "forward"):
            # Remote page references expire on navigation; closed-page trees must not accumulate.
            for key, value in list(self.objects.items()):
                if key not in self.scopes and type(value).__name__ in ("Element", "StaticElement", "FrameView", "BackendElement"):
                    self.objects.pop(key, None)
        if method in ("capture_resources", "block"):
            context = result
            result = context.__enter__()
            encoded = self.encode(result)
            self.scopes[encoded["$object"]] = context
            return encoded
        if method == "pump_events":
            for value in self.objects.values():
                if type(value).__name__ == "ResourceTracker" and len(value._order) > 2000:
                    raise errors.Busy("资源事件上限已满，请缩小捕获范围")
        return self.encode(result)

    def run(self):
        context_token = current.set(self.user)
        try:
            with ExitStack() as stack:
                profile = self.supervisor.controller.profile(self.row["profile"], self.user)
                if self.supervisor.factory:
                    session = stack.enter_context(self.supervisor.factory(profile, self.options))
                    self.owns = True
                else:
                    store = DatabaseLease()
                    key = "service:" + profile["environment"] + ":" + profile["source_id"]
                    token = store.acquire(key, ttl=120)
                    if not token:
                        raise errors.Busy("浏览器实例已被占用")
                    self.lease = (store, key, token)
                    self.owns = True
                if not self.supervisor.factory and profile["environment"] == "native":
                    native = NativeSupervisor()
                    running = native.start(profile["source_id"])
                    endpoint = Plain(f"http://127.0.0.1:{running['port']}").endpoint()
                    transport = stack.enter_context(Transport.connect(endpoint.ws_url))
                    session = stack.enter_context(Session.create(transport, **self.options))
                elif not self.supervisor.factory:
                    manager = stack.enter_context(manager_for(profile["environment"]))
                    # Capacity failures must reach the API before Pool retries a launch.
                    manager.governor.reserve(manager, profile["source_id"])
                    inst = stack.enter_context(manager.lease(instance_id=profile["source_id"], timeout=30))
                    session = stack.enter_context(inst.session(**self.options))
                self.transport = getattr(session, "transport", None)
                self.supervisor.db.put("profiles", profile["id"], {**profile, "state": "running", "touched": self.db.now()})
                self.supervisor.update(self.row["id"], state="running")
                self.ready.set_result(True)
                while not self.stop.is_set():
                    try:
                        body, future = self.queue.get(timeout=.25)
                    except queue.Empty:
                        continue
                    try:
                        result = self.execute(session, body)
                        if len(__import__("json").dumps(result)) > 8 * 1024 * 1024:
                            raise errors.Busy("结果超过 8MB 上限")
                        future.set_result(result)
                    except BaseException as exc:
                        self.last_error = f"{type(exc).__name__}: {exc}"[:500]
                        future.set_exception(exc)
                for context in list(self.scopes.values()):
                    context.__exit__(None, None, None)
                self.scopes.clear()
        except BaseException as exc:
            if not self.ready.done():
                self.ready.set_exception(exc)
        finally:
            for context in list(self.scopes.values()):
                with suppress(Exception):
                    context.__exit__(None, None, None)
            try:
                self.supervisor.update(self.row["id"], state="cleanup_pending")
                profile = self.supervisor.controller.profile(self.row["profile"], self.user)
                if self.owns and not self.supervisor.factory:
                    self.supervisor.controller.cleanup(profile, delete=profile["ephemeral"])
                self.supervisor.update(self.row["id"], state="released")
            except Exception:
                pass
            while not self.queue.empty():
                _, future = self.queue.get_nowait()
                future.set_exception(errors.SessionLost("会话已结束"))
            self.done.set()
            self.objects.clear()
            self.scopes.clear()
            self.gateway = None
            self.transport = None
            current.reset(context_token)

    def close(self):
        self.stop.set()
        if self.transport:
            self.transport.close()

    def heartbeat(self):
        if self.lease:
            store, key, token = self.lease
            if not store.renew(key, token, ttl=120):
                self.close()
                raise errors.LeaseLost("浏览器租约已失效")


class Supervisor:
    def __init__(self, *, factory=None):
        self.db, self.identity, self.controller = database(), Identity(), Controller()
        self.factory = factory
        self.actors = {}
        self.lock = threading.RLock()
        self.worker = uuid.uuid4().hex

    def update(self, sid, **changes):
        with self.db.state("service-sessions") as state:
            previous = state["records"][sid].get("state")
            state["records"][sid].update(changes)
            pid = state["records"][sid]["profile"]
        if changes.get("state") and changes["state"] != previous:
            from .fleet import Fleet
            labels = {"running": "会话已启动", "released": "会话已释放，浏览器清理完成", "cleanup_pending": "正在停止浏览器并回收资源"}
            Fleet(self).record(pid, changes["state"], labels.get(changes["state"], changes["state"]))

    def get(self, sid, user):
        with self.db.state("service-sessions") as state:
            row = state["records"].get(sid)
            if not row or (row["owner"] != user.user_id and not (user.role == "admin" and "manage" in user.scopes)):
                raise errors.NotFound("会话不存在或没有权限")
            return dict(row)

    def open(self, body, user, request_id):
        if self.db.storage_pending:
            raise errors.Busy("数据库正在切换，请重启 Sleight 后继续操作")
        if "execute" not in user.scopes:
            raise PermissionError("凭据没有执行权限")
        if not request_id or len(request_id) > 120:
            raise ValueError("会话需要有效 Idempotency-Key")
        profile = self.controller.profile(body["profile"], user)
        source_key = profile["environment"] + ":" + profile["source_id"]
        default = self.identity.user(user.user_id) or {"limit": 3}
        with self.lock, self.db.state("service-sessions") as state:
            if self.db.storage_pending:
                raise errors.Busy("数据库正在切换，请重启 Sleight 后继续操作")
            previous = next((r for r in state["records"].values() if r["owner"] == user.user_id and r["request"] == request_id), None)
            if previous:
                return dict(previous)
            if sum(r["owner"] == user.user_id and r["state"] != "released" for r in state["records"].values()) >= default["limit"]:
                raise errors.Busy("用户并发额度已满")
            if any(r.get("source_key", r["profile"]) == source_key and r["state"] != "released" for r in state["records"].values()):
                raise errors.Busy("实例已被占用")
            if source_key in state.get("deletions", {}):
                raise errors.Busy("实例正在删除或等待清理，不能开始会话")
            sid = uuid.uuid4().hex
            row = {"id": sid, "owner": user.user_id, "token": user.token_id, "profile": body["profile"],
                   "state": "starting", "request": request_id, "expires": self.db.now() + 120, "created": self.db.now(),
                   "worker": self.worker, "source_key": source_key}
            state["records"][sid] = row
            state["owners"][self.worker] = self.db.now() + 120
        options = {k: v for k, v in body.get("options", {}).items() if k in {"human", "track_network", "track_runtime"}}
        if isinstance(options.get("human"), dict):
            from ..core.human import HumanProfile
            options["human"] = HumanProfile(**options["human"])
        actor = Actor(self, row, user, options)
        with self.lock:
            self.actors[sid] = actor
        try:
            actor.ready.result(timeout=90)
        except BaseException:
            actor.close()
            raise
        return self.get(sid, user)

    def heartbeat(self, sid, user):
        row = self.get(sid, user)
        if row["state"] not in ("running", "starting"):
            raise errors.SessionLost("会话不再运行")
        self.update(sid, expires=self.db.now() + 120)
        actor = self.actors.get(sid)
        if actor:
            actor.heartbeat()
        return {"ok": True}

    def call(self, sid, user, body):
        row = self.get(sid, user)
        if row["state"] != "running":
            raise errors.SessionLost("会话已失效")
        actor = self.actors.get(sid)
        if not actor:
            raise errors.SessionLost("服务已重启，请申请新会话")
        self.heartbeat(sid, user)
        return actor.submit(body)

    def close(self, sid, user):
        self.get(sid, user)
        actor = self.actors.get(sid)
        if actor:
            actor.close()
            actor.done.wait(10)
        return {"ok": True}

    def delete_profile(self, pid, user):
        profile = self.controller.profile(pid, user)
        source_key = profile["environment"] + ":" + profile["source_id"]
        with self.db.state("service-sessions") as state:
            if any(r["state"] != "released" and (r["profile"] == pid or r.get("source_key") == source_key)
                   for r in state["records"].values()):
                raise errors.Busy("请先结束该实例的会话，等待清理完成")
            if source_key in state.setdefault("deletions", {}):
                raise errors.Busy("实例正在删除或等待清理")
            state["deletions"][source_key] = pid
        self.db.put("profiles", pid, {**profile, "state": "cleanup_pending", "touched": self.db.now()})
        self.controller.cleanup(profile, delete=True)
        with self.db.state("service-sessions") as state:
            state["deletions"].pop(source_key, None)
        return {"ok": True}

    def sweep(self):
        self.identity.prune()
        with self.db.state("service-sessions") as state:
            for sid, actor in list(self.actors.items()):
                if actor.done.is_set() and state["records"].get(sid, {}).get("state") == "released":
                    self.actors.pop(sid, None)
            released = [r for r in state["records"].values() if r["state"] == "released"]
            for record in sorted(released, key=lambda r: r["created"])[:-2000]:
                state["records"].pop(record["id"], None)
            rows = [dict(r) for r in state["records"].values() if r["state"] != "released"]
            state["owners"][self.worker] = self.db.now() + 120
            owners = dict(state["owners"])
        for row in rows:
            actor = self.actors.get(row["id"])
            invalid = row["expires"] <= self.db.now() or (row["token"] and not self.identity.valid_id(row["token"]))
            if actor and invalid:
                actor.close()
            if actor and not invalid:
                actor.heartbeat()
            abandoned = not actor and owners.get(row.get("worker"), 0) < self.db.now()
            if abandoned or (actor and actor.done.is_set()):
                with self.db.state("service-sessions") as state:
                    stored = state["records"][row["id"]]
                    if stored["state"] == "released" or (stored["state"] == "cleaning" and stored.get("claim_until", 0) > self.db.now()):
                        if actor and actor.done.is_set():
                            self.actors.pop(row["id"], None)
                        continue
                    claim = uuid.uuid4().hex
                    stored.update(state="cleaning", claim=claim, claim_until=self.db.now() + 660)
                try:
                    profile = self.db.get("profiles", row["profile"])
                    if profile and not self.factory:
                        self.controller.cleanup(profile, delete=profile["ephemeral"])
                    with self.db.state("service-sessions") as state:
                        stored = state["records"].get(row["id"], {})
                        if stored.get("claim") == claim:
                            stored.update(state="released", claim_until=0)
                            self.actors.pop(row["id"], None)
                except Exception:
                    with self.db.state("service-sessions") as state:
                        stored = state["records"].get(row["id"], {})
                        if stored.get("claim") == claim:
                            stored.update(state="cleanup_pending", claim_until=0)
        active = {r["profile"] for r in rows}
        for profile in self.db.list("profiles"):
            if profile["id"] not in active and (profile["ephemeral"] or profile.get("state") == "cleanup_pending") and profile["touched"] < self.db.now() - 120:
                with suppress(Exception):
                    self.controller.cleanup(profile, delete=True)
                    with self.db.state("service-sessions") as state:
                        key = profile["environment"] + ":" + profile["source_id"]
                        if state.get("deletions", {}).get(key) == profile["id"]:
                            state["deletions"].pop(key, None)
        with self.db.state("profile-requests") as state:
            for key, row in list(state["records"].items()):
                if row["time"] < self.db.now() - 86400 and not self.db.get("profiles", row["id"]):
                    del state["records"][key]

    def shutdown(self):
        for actor in list(self.actors.values()):
            actor.close()
        for actor in list(self.actors.values()):
            actor.done.wait(10)
