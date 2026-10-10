"""FastAPI 应用：用界面做部署和运维。

**这个界面能在目标机上执行 ssh 和 docker 命令**，等于一个远程执行入口。所以：

* 默认只监听 ``127.0.0.1``；
* ``serve`` 始终启用鉴权，首次启动签发管理员 token；
* Manager token 留在目标机的 ``.env``，用户凭据与其他密钥由统一数据库管理。

拉镜像动辄几分钟，所以部署、下发、验证这些长动作都返回一个 job id，进度用 SSE
（``/api/jobs/{id}/events``）推。界面因此是流式的，不是一个转圈等超时的 POST。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable, Iterator
from dataclasses import asdict
from pathlib import Path
from typing import Any

from ...core.errors import Busy, SleightError
from ..engine import Deployer
from ..errors import DeployError
from ..ops import ExtensionOps, ProfileOps, extension_paths
from ..preflight import parse_mem_total_kb
from ..presets import (
    DEPLOY_TEMPLATES,
    FIELD_HELP,
    PROFILE_PRESETS,
    profile_regions,
    profile_spec_from,
)
from ..spec import DEFAULT_IMAGE, DeploySpec
from ..store import Deployment, Host, Store
from .auth import UIAuth
from .jobs import Jobs


def _format_mem(result: Any) -> str:
    """``/proc/meminfo`` → ``"3.8 GB"``。和 preflight 用同一个解析函数。"""
    total = parse_mem_total_kb(result.text)
    return f"{total / 1024 / 1024:.1f} GB" if total else "读不出来"

log = logging.getLogger("sleight.deploy.api")

__all__ = ["create_app", "serve"]

INDEX = Path(__file__).with_name("static") / "index.html"
#: job 记录保留多久（秒）。够看完日志，又不至于让长跑的进程无限涨
JOB_TTL = 3600.0


# fastapi 必须在**模块级**导入。这个模块用了 PEP 563（``from __future__ import
# annotations``），FastAPI 解析路由签名时只会在模块全局里找名字 —— 在 create_app()
# 里局部 import 的话，``request: Request`` 会被当成一个未知类型的查询参数，
# 每个请求都 422。
try:
    from fastapi import Body, Depends, FastAPI, HTTPException, Query, Request, Response
    from fastapi.responses import HTMLResponse, StreamingResponse
    from fastapi.staticfiles import StaticFiles

    HAS_FASTAPI = True
except ModuleNotFoundError:                                # pragma: no cover - 取决于环境
    HAS_FASTAPI = False
    Body = Depends = FastAPI = HTTPException = Query = Request = None      # type: ignore[assignment]
    HTMLResponse = StreamingResponse = None                                # type: ignore[assignment]


def _require_fastapi() -> None:
    if not HAS_FASTAPI:                                    # pragma: no cover - 取决于环境
        raise DeployError(
            "FastAPI is missing from this installation; repair it with: pip install sleight"
        )


# --------------------------------------------------------------------------- #
# job
# --------------------------------------------------------------------------- #



# --------------------------------------------------------------------------- #
# 应用
# --------------------------------------------------------------------------- #


def create_app(*, token: str | None = None, runtime: bool = True, require_auth: bool = False) -> Any:
    """建 FastAPI 应用。

    :param token: 服务使用 ``X-Sleight-Token``；浏览器登录后使用 HttpOnly cookie。
        URL 查询参数不接受口令。
    :returns: ``fastapi.FastAPI`` 实例
    :raises DeployError: 没装 fastapi
    """
    _require_fastapi()
    jobs = Jobs()
    auth = UIAuth(token, required=require_auth)
    guard = auth.guard

    app = FastAPI(
        title="sleight deploy",
        description="部署与运维 CloakBrowser Manager",
        version=_version(),
        dependencies=[Depends(guard)],
    )

    from .agents import setup_agents
    from .runtime import setup_runtime
    from .viewer import setup_viewer
    setup_runtime(app, jobs, enabled=runtime)
    setup_agents(app)
    from ...service.api import setup_service
    setup_service(app, auth)

    @app.middleware("http")
    async def prefix(request: Request, call_next):
        started = time.monotonic()
        from ...service.identity import current
        principal = auth.resolve(request.headers, request.cookies)
        context_token = current.set(principal)
        value = request.headers.get("x-forwarded-prefix", "")
        if (value and request.headers.get("x-sleight-token") and principal
                and value.startswith("/") and ".." not in value and not value.endswith("/")):
            request.scope["root_path"] = value
            # ASGI mounts strip root_path from the full path, including /assets.
            path = request.scope["path"]
            if path != value and not path.startswith(value + "/"):
                request.scope["path"] = value + path
                request.scope["raw_path"] = value.encode() + request.scope.get("raw_path", path.encode())
        try:
            response = await call_next(request)
        finally:
            current.reset(context_token)
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            # Record method/path/status only; proxy URLs, bodies and headers can contain secrets.
            try:
                await asyncio.to_thread(Store().log_event, "console", "api", ok=response.status_code < 400,
                                        detail=f"user={principal.user_id if principal else 'anonymous'} {request.method} {request.url.path} => {response.status_code}")
                import uuid

                from ...service.database import database
                def record():
                    with database().state("audit") as state:
                        key = uuid.uuid4().hex
                        state["records"][key] = {"id": key, "time": time.time(), "user": principal.user_id if principal else "anonymous",
                            "token_id": principal.token_id if principal else "", "method": request.method, "path": request.url.path,
                            "status": response.status_code, "duration_ms": round((time.monotonic() - started) * 1000)}
                        for old in list(state["records"])[:-5000]:
                            del state["records"][old]
                await asyncio.to_thread(record)
            except Exception:
                log.warning("Unable to write API audit event")
        return response

    app.state.auth = auth
    app.state.jobs = jobs
    app.mount("/assets", StaticFiles(directory=INDEX.parent / "assets", check_dir=False), name="assets")

    @app.post("/api/auth/login")
    def login(request: Request, response: Response, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        return auth.login(request, response, str(body.get("token") or ""))

    @app.post("/api/auth/logout")
    def logout(request: Request, response: Response) -> dict[str, Any]:
        principal = auth.resolve({}, request.cookies)
        if principal and principal.token_id:
            auth.identity.revoke(principal.token_id)
        auth.sessions.pop(request.cookies.get("sleight_session", ""), None)
        response.delete_cookie("sleight_session", path=(request.scope.get("root_path", "") or "") + "/")
        return {"ok": True}

    # ---------------------------------------------------------------- #

    def store() -> Store:
        return Store()

    def _ref(name: str, deployment: str | None) -> str:
        """``主机`` + 可选部署名 → ``主机/部署名``。

        部署名走**查询参数**而不是路径段：路径参数不匹配斜杠，``local/default``
        塞进 ``/api/hosts/{name}/status`` 会直接 404。
        """
        return f"{name}/{deployment}" if deployment else name

    def _entry(ref: str) -> tuple[Host, Deployment]:
        """``主机`` 或 ``主机/部署名`` → (主机, 部署记录)。

        界面里那个"本机"是虚拟列出来的，头一次真对它动手时才落库 —— 否则每个操作
        都会因为"库里没这条"而 404。
        """
        db = store()
        if ref.split("/")[0] == "local" and db.get_host("local") is None:
            db.ensure_local()
        try:
            deployment = db.resolve(ref)
            return db.require_host(deployment.host), deployment
        except DeployError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    def deployer(
        ref: str,
        *,
        on_progress: Callable[[str], None] | None = None,
        **overrides: Any,
    ) -> tuple[Deployer, Deployment]:
        host, deployment = _entry(ref)
        spec = deployment.spec.replace(**{k: v for k, v in overrides.items() if v is not None})
        dep = Deployer(spec, host.runner(), sudo=host.sudo, on_progress=on_progress)
        return dep, deployment

    def record(deployment: Deployment, kind: str, *, ok: bool, detail: str = "",
               image: str = "", status: dict[str, Any] | None = None) -> None:
        """把动作记进库。**永远不抛** —— 记不上账不该让成功的动作变成失败。"""
        try:
            db = store()
            if ok and image:
                db.record_deploy(deployment.host, deployment.name, image=image, status=status or {})
            db.log_event(deployment.host, kind, ok=ok, deployment=deployment.name, detail=detail)
        except Exception:                                  # 记账不该阻断
            log.debug("记流水失败", exc_info=True)

    def _persist(deployment: Deployment, spec: DeploySpec, say: Callable[[str], None]) -> None:
        """把这次实际用的 spec 写回库。失败只提示，不让已经成功的部署变成失败。"""
        try:
            current = store().get_deployment(deployment.host, deployment.name)
            if current is not None and current.spec == spec:
                return
            store().put_deployment(deployment.host, deployment.name, spec)
            say(f"记录已更新：{deployment.ref} → {spec.dir}:{spec.port}")
        except Exception as exc:
            say(f"注意：部署成功了，但本地记录没更新（{exc}）。"
                f"记录里还写着旧参数，之后的操作会去错地方。")
            log.warning("could not persist spec for %s", deployment.ref, exc_info=True)

    def wrap(fn: Callable[[], Any]) -> Any:
        """把库里的异常翻成 HTTP 错误，而不是 500 + 一页 traceback。"""
        try:
            return fn()
        except Busy as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except (DeployError, SleightError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=f"{type(exc).__name__}: {exc}") from exc

    # ---------------------------------------------------------------- #
    # 静态页
    # ---------------------------------------------------------------- #

    @app.get("/", include_in_schema=False)
    def index() -> HTMLResponse:
        if not INDEX.is_file():                            # pragma: no cover - 打包异常
            return HTMLResponse("<h1>index.html missing from the wheel</h1>", status_code=500)
        return HTMLResponse(INDEX.read_text(encoding="utf-8"))

    @app.get("/api/defaults")
    def defaults() -> dict[str, Any]:
        return {
            "version": _version(),
            "default_image": DEFAULT_IMAGE,
            "spec": asdict(DeploySpec()),
            "auth": bool(token),
            # 模板和字段解释只在后端定义一份，前端渲染它
            "deploy_templates": [t.to_dict() for t in DEPLOY_TEMPLATES],
            "profile_presets": [p.to_dict() for p in PROFILE_PRESETS],
            "profile_regions": profile_regions(),
            "help": {k: v.to_dict() for k, v in FIELD_HELP.items()},
        }

    # ---------------------------------------------------------------- #
    # 主机
    # ---------------------------------------------------------------- #

    @app.post("/api/probe")
    def probe_host(body: dict[str, Any] = Body(default={})) -> dict[str, Any]:
        """**保存之前**先测一下这台机连不连得上、有没有 docker。

        引导流程要的就是这个：填完连接信息立刻能验证，而不是保存了、部署了、
        等超时了才发现 key 不对。所以它不碰库，也不需要先有主机记录。
        """
        host = Host(
            name="probe",
            ssh=str(body.get("ssh") or ""),
            port=int(body["port"]) if body.get("port") else None,
            identity=str(body.get("identity") or ""),
            strict_host_key="accept-new" if body.get("accept_new") else "",
        )
        runner = host.runner()
        steps: list[dict[str, Any]] = []

        def step(name: str, argv: list[str], parse=lambda r: r.text) -> bool:
            result = runner.run(argv, timeout=25)
            steps.append({
                "name": name, "ok": result.ok,
                "detail": parse(result) if result.ok
                else (result.err.strip() or result.out.strip() or f"exit {result.code}"),
            })
            return result.ok

        try:
            if not step("连接", ["uname", "-sm"]):
                return {"ok": False, "steps": steps,
                        "hint": "连不上。检查地址、端口和私钥；BatchMode 是开着的，"
                                "所以不会弹密码框而是直接失败。"}
            if not step("docker", ["docker", "version", "--format", "{{.Server.Version}}"]):
                return {"ok": False, "steps": steps,
                        "hint": "目标机上没有可用的 docker。装好并把当前用户加进 docker 组"
                                "（usermod -aG docker $USER，然后重新登录）。"}
            step("compose", ["docker", "compose", "version", "--short"])
            # 走 /proc/meminfo 而不是 free -g：后者向下取整，3.8 GB 的机器显示成
            # "3 GB"，比 preflight 少报近 1 GB。同一台机两个界面报不同的数字，
            # 使用者只会怀疑工具。这里和 preflight 共用同一个解析函数
            step("内存", ["cat", "/proc/meminfo"], parse=_format_mem)
            step("当前用户", ["id", "-un"])
        except (DeployError, SleightError) as exc:
            return {"ok": False, "steps": steps, "hint": f"{type(exc).__name__}: {exc}"}
        finally:
            runner.close()
        return {"ok": all(s["ok"] for s in steps), "steps": steps, "hint": ""}

    @app.get("/api/hosts")
    def list_hosts() -> list[dict[str, Any]]:
        db = store()
        deployments = db.deployments()
        out = [
            {**h.to_dict(),
             "deployments": [d.to_dict() for d in deployments if d.host == h.name]}
            for h in db.hosts()
        ]
        if not any(h["name"] == "local" for h in out):
            # 本机永远可选：装了 docker 就能一键部署，不必先配主机
            out.insert(0, {
                **Host(name="local").to_dict(), "implicit": True,
                "deployments": [],
            })
        return out

    @app.post("/api/hosts")
    def add_host(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """记一台主机，顺带建一个叫 default 的部署 —— 没有部署记录的主机没法用。"""
        name = str(body.get("name") or "").strip()
        if not name:
            raise HTTPException(status_code=400, detail="name is required")
        spec_fields = {k: v for k, v in (body.get("deploy") or {}).items() if v not in (None, "")}
        db = store()
        old = db.get_host(name)
        if old and not body.get("replace"):
            if (str(body.get("ssh") or ""), bool(body.get("sudo"))) != (old.ssh, old.sudo):
                raise HTTPException(409, "主机已登记；请使用原连接配置或不同主机代号")
            return {"ok": True, "path": str(db.path), "ref": f"{name}/{body.get('deployment') or 'default'}"}
        wrap(lambda: db.put_host(Host(
            name=name,
            ssh=str(body.get("ssh") or ""),
            port=int(body["port"]) if body.get("port") else None,
            identity=str(body.get("identity") or ""),
            sudo=bool(body.get("sudo")),
            strict_host_key="accept-new" if body.get("accept_new") else "",
            notes=str(body.get("notes") or ""),
        )))
        deployment = str(body.get("deployment") or "default")
        if not body.get("host_only"):
            wrap(lambda: db.put_deployment(name, deployment, DeploySpec.from_dict(spec_fields)))
        return {"ok": True, "path": str(db.path), "ref": f"{name}/{deployment}"}

    @app.delete("/api/hosts/{name}")
    def remove_host(name: str) -> dict[str, Any]:
        db = store()
        wrap(lambda: db.delete_host(name))
        return {"ok": True}

    # ---------------------------------------------------------------- #
    # 部署记录（一台机上的 N 个 Manager）
    # ---------------------------------------------------------------- #

    @app.get("/api/deployments")
    def list_deployments(host: str | None = None) -> list[dict[str, Any]]:
        return [d.to_dict() for d in store().deployments(host=host)]

    @app.post("/api/deployments")
    def add_deployment(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        host = str(body.get("host") or "").strip()
        name = str(body.get("name") or "").strip()
        if not host or not name:
            raise HTTPException(status_code=400, detail="host and name are required")
        spec_fields = {k: v for k, v in (body.get("spec") or {}).items() if v not in (None, "")}
        db = store()
        if db.get_deployment(host, name) and not body.get("replace"):
            raise HTTPException(409, "部署代号已存在，请选择新的代号")
        wanted = str(spec_fields.get("dir") or DeploySpec().dir).rstrip("/")
        if any(d.name != name and d.spec.dir.rstrip("/") == wanted for d in db.deployments(host=host)):
            raise HTTPException(409, "此目录已属于另一个部署，请选择独立目录")
        wrap(lambda: db.put_deployment(host, name, DeploySpec.from_dict(spec_fields)))
        return {"ok": True, "ref": f"{host}/{name}"}

    @app.delete("/api/deployments/{host}/{name}")
    def remove_deployment(host: str, name: str) -> dict[str, Any]:
        wrap(lambda: store().delete_deployment(host, name))
        return {"ok": True}

    @app.post("/api/hosts/{name}/import")
    def import_deployment(name: str, body: dict[str, Any] = Body(...)):
        from ..importer import import_spec
        db = store()
        if name == "local":
            db.ensure_local()
        host = db.require_host(name)
        deployment_name = str(body.get("name") or "default")
        if db.get_deployment(name, deployment_name) and not body.get("replace"):
            raise HTTPException(409, "部署代号已存在，请选择新的代号")
        spec = wrap(lambda: import_spec(host.runner(), str(body.get("dir") or ""),
                    filename=str(body.get("filename") or "docker-compose.yaml"),
                    service=str(body.get("service") or "manager")))
        return wrap(lambda: db.put_deployment(name, deployment_name, spec).to_dict())

    @app.get("/api/hosts/{name}/capacity")
    def capacity(name: str, deployment: str | None = None):
        dep, _ = deployer(_ref(name, deployment))
        with dep.connect() as manager:
            return {**manager.governor.snapshot(), "system": manager.system_status()}

    @app.get("/api/events")
    def list_events(host: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        return [e.to_dict() for e in store().events(host=host, limit=limit)]

    # ---------------------------------------------------------------- #
    # 部署
    # ---------------------------------------------------------------- #

    @app.post("/api/hosts/{name}/preflight")
    def preflight(
        name: str, deployment: str | None = None, body: dict[str, Any] = Body(default={})
    ) -> dict[str, Any]:
        dep, _ = deployer(_ref(name, deployment), **_spec_overrides(body))
        plan = wrap(dep.plan)
        return {
            "blocked": plan.blocked,
            "up_to_date": plan.up_to_date,
            "checks": [
                {"name": c.name, "level": c.level.value, "detail": c.detail, "hint": c.hint}
                for c in plan.checks
            ],
            "changes": plan.changes,
            "warnings": plan.warnings,
            "commands": [list(c) for c in plan.commands],
            "files": plan.files,
        }

    @app.get("/api/hosts/{name}/status")
    def status(name: str, deployment: str | None = None) -> dict[str, Any]:
        return wrap(deployer(_ref(name, deployment))[0].status)

    @app.get("/api/hosts/{name}/logs")
    def logs(name: str, tail: int = 200, deployment: str | None = None) -> dict[str, Any]:
        return {"text": wrap(lambda: deployer(_ref(name, deployment))[0].logs(tail=tail))}

    @app.post("/api/hosts/{name}/deploy")
    def deploy(
        name: str, deployment: str | None = None, body: dict[str, Any] = Body(default={})
    ) -> dict[str, Any]:
        overrides = _spec_overrides(body)
        force = bool(body.get("force"))
        ref = _ref(name, deployment)

        def work(say: Callable[[str], None]) -> dict[str, Any]:
            dep, entry = deployer(ref, on_progress=say, **overrides)
            result = dep.apply(force=force)
            # **把真正部署下去的 spec 存回库里。** 界面上「部署」那一页是当作"改这个
            # 部署的参数"来呈现的：改了目录点部署，容器确实去了新目录，但记录还指着
            # 旧的 —— 之后取 token、看实例、销毁全都会去错地方，而真正的部署变成
            # 谁也不认识的孤儿。真机上就这么撞出来的
            if overrides:
                _persist(entry, dep.spec, say)
            record(entry, "deploy", ok=True, image=dep.spec.image, status=result.status,
                   detail="有变更" if result.changed else "无变更")
            return {
                "changed": result.changed,
                "summary": result.summary,
                "status": result.status,
                "url": dep.spec.bound_url,
                "token_hint": f"{result.token[:8]}…",
                "env_path": dep.spec.env_path,
            }

        return {"job": jobs.start("deploy", name, work).id}

    @app.post("/api/hosts/{name}/upgrade")
    def upgrade(
        name: str, deployment: str | None = None, body: dict[str, Any] = Body(...)
    ) -> dict[str, Any]:
        ref = _ref(name, deployment)
        image = str(body.get("image") or "").strip()
        if not image:
            raise HTTPException(status_code=400, detail="image is required")
        backup = bool(body.get("backup", True))

        def work(say: Callable[[str], None]) -> dict[str, Any]:
            dep, entry = deployer(ref, on_progress=say)
            result = dep.upgrade(image, backup=backup)
            record(entry, "upgrade", ok=True, image=image, status=result.status,
                   detail=f"→ {image}")
            return {"summary": result.summary, "status": result.status}

        return {"job": jobs.start("upgrade", name, work).id}

    @app.get("/api/hosts/{name}/token")
    def token_of(name: str, deployment: str | None = None) -> dict[str, Any]:
        """取回完整的 AUTH_TOKEN。

        它就在目标机的 ``.env``（600）里，能操作这个界面的人本来就能 SSH 上去读到。
        Manager 的 Web UI 要用它 —— 那边没有用户名密码，token 就是唯一凭据。
        """
        dep, _ = deployer(_ref(name, deployment))
        value = wrap(dep.existing_token)
        if not value:
            raise HTTPException(
                status_code=404, detail=f"{dep.spec.env_path} 里没有 AUTH_TOKEN，还没部署过？"
            )
        return {"token_hint": value[:4] + "…", "url": dep.spec.local_url, "env_path": dep.spec.env_path}

    @app.post("/api/hosts/{name}/rollback")
    def rollback(name: str, deployment: str | None = None) -> dict[str, Any]:
        ref = _ref(name, deployment)

        def work(say: Callable[[str], None]) -> dict[str, Any]:
            dep, entry = deployer(ref, on_progress=say)
            result = dep.rollback()
            record(entry, "rollback", ok=True, image=dep.spec.image, status=result.status,
                   detail=f"→ {dep.spec.image}")
            return {"summary": result.summary, "status": result.status}

        return {"job": jobs.start("rollback", ref, work).id}

    @app.post("/api/hosts/{name}/destroy")
    def destroy(
        name: str, deployment: str | None = None, body: dict[str, Any] = Body(default={})
    ) -> dict[str, Any]:
        """停并删容器。``purge_data`` 要额外把部署名原样打一遍才认。

        删 ``data/`` 是不可逆的：profile 数据库、指纹种子、Cookie 和全部登录态都在
        里面。命令行那边要 ``--purge-data --yes``，界面这边就得打字确认 —— 一个能被
        误点的按钮不该有这种后果。
        """
        ref = _ref(name, deployment)
        purge = bool(body.get("purge_data"))
        purge_image = bool(body.get("purge_image"))
        if purge:
            # 比对**解析之后**的 host/deployment，不是调用方随手写的那串 ——
            # 确认的是"要销毁哪个东西"，不该取决于你怎么寻址它
            _, entry = _entry(ref)
            if str(body.get("confirm") or "") != entry.ref:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"要删 data/ 就把 {entry.ref!r} 原样打一遍 —— "
                        "登录态和 profile 删了回不来"
                    ),
                )

        def work(say: Callable[[str], None]) -> dict[str, Any]:
            dep, entry = deployer(ref, on_progress=say)
            dep.destroy(purge_data=purge, purge_image=purge_image)
            gone = ["容器"] + (["data/"] if purge else []) + (["镜像"] if purge_image else [])
            record(entry, "destroy", ok=True, detail="删了：" + "、".join(gone))
            return {"purged": purge, "purged_image": purge_image}

        return {"job": jobs.start("destroy", ref, work).id}

    @app.post("/api/hosts/{name}/backup")
    def backup(name: str, deployment: str | None = None) -> dict[str, Any]:
        ref = _ref(name, deployment)

        def work(say: Callable[[str], None]) -> dict[str, Any]:
            dep, entry = deployer(ref, on_progress=say)
            archive = dep.backup()
            record(entry, "backup", ok=True, detail=archive)
            return {"archive": archive}

        return {"job": jobs.start("backup", name, work).id}

    # ---------------------------------------------------------------- #
    # profile
    # ---------------------------------------------------------------- #

    @app.get("/api/hosts/{name}/profiles")
    def profiles(name: str, deployment: str | None = None) -> list[dict[str, Any]]:
        raw = wrap(ProfileOps(deployer(_ref(name, deployment))[0]).list)
        for p in raw:
            p["extension_paths"] = p.get("extension_paths") or extension_paths([str(a) for a in (p.get("launch_args") or [])])
            from ...runtime.config import mask_proxy
            p["proxy"] = mask_proxy(p.get("proxy", ""))
        return raw

    @app.post("/api/hosts/{name}/profiles")
    def create_profile(
        name: str, deployment: str | None = None, body: dict[str, Any] = Body(...)
    ) -> dict[str, Any]:
        """按身份模板建一个实例。**幂等**：同名的会被更新而不是再建一个。

        ``auto_launch`` 一律 False —— 建的时候不该顺手拉起一个浏览器占着内存，
        要用时 ``lease()`` 会自己拉。
        """
        body = app.state.templates.apply(body)
        profile_name = str(body.get("name") or "").strip()
        if not profile_name:
            raise HTTPException(status_code=400, detail="实例得有个名字")
        overrides: dict[str, Any] = {
            "proxy": body.get("proxy") or None,
            "extension_paths": tuple(body.get("extension_paths") or []),
            "geoip": bool(body.get("geoip")),
            "notes": body.get("notes"),
            "tags": tuple(t.strip() for t in str(body.get("tags") or "").split(",") if t.strip()),
        }
        for key in ("screen_width", "screen_height", "fingerprint_seed"):
            if body.get(key):
                overrides[key] = int(body[key])
        spec = wrap(lambda: profile_spec_from(
            str(body.get("preset") or "windows"), profile_name,
            str(body.get("region") or "us_east"), **overrides
        ))
        dep, _ = deployer(_ref(name, deployment))

        def make() -> dict[str, Any]:
            with dep.connect() as mgr:
                info = mgr.ensure_profile(spec)
                return {"id": info.id, "name": info.name, "tags": sorted(info.tags)}

        return wrap(make)

    @app.delete("/api/hosts/{name}/profiles/{pid}")
    def delete_profile(
        name: str, pid: str, confirm: str = "", deployment: str | None = None
    ) -> dict[str, Any]:
        """删一个实例。**要把它的名字原样打一遍。**

        删 profile 连带删掉 ``user_data_dir`` —— Cookie 和登录态一起没，不可逆。
        停止实例不会丢这些，删才会。
        """
        dep, _ = deployer(_ref(name, deployment))

        def drop() -> dict[str, Any]:
            with dep.connect() as mgr:
                raw = next((p for p in mgr.list_profiles() if str(p.get("id")) == pid), None)
                if raw is None:
                    raise HTTPException(status_code=404, detail=f"没有 id 为 {pid} 的实例")
                if confirm != (raw.get("name") or ""):
                    raise HTTPException(
                        status_code=400,
                        detail=(
                            f"要删就把实例名 {raw.get('name')!r} 原样打一遍 —— "
                            "删 profile 连登录态一起删，回不来"
                        ),
                    )
                mgr.delete_profile(pid, force=True)
                return {"ok": True, "name": raw.get("name")}

        return wrap(drop)

    @app.post("/api/hosts/{name}/profiles/{pid}/{action}")
    def profile_action(
        name: str, pid: str, action: str, deployment: str | None = None
    ) -> dict[str, Any]:
        ops = ProfileOps(deployer(_ref(name, deployment))[0])
        if action == "launch":
            return {"id": wrap(lambda: ops.launch(pid))}
        if action == "stop":
            return {"id": wrap(lambda: ops.stop(pid))}
        if action == "stop-all":
            return {"stopped": wrap(ops.stop_all)}
        raise HTTPException(status_code=400, detail=f"unknown action {action!r}")

    # ---------------------------------------------------------------- #
    # 插件
    # ---------------------------------------------------------------- #

    @app.get("/api/hosts/{name}/extensions")
    def extensions(name: str, deployment: str | None = None) -> list[dict[str, Any]]:
        ops = ExtensionOps(deployer(_ref(name, deployment))[0])
        return [asdict(e) for e in wrap(ops.list_installed)]

    @app.get("/api/hosts/{name}/extensions/drift")
    def drift(name: str, deployment: str | None = None) -> dict[str, Any]:
        return wrap(ExtensionOps(deployer(_ref(name, deployment))[0]).drift)

    @app.post("/api/hosts/{name}/extensions/push")
    def push(
        name: str, deployment: str | None = None, body: dict[str, Any] = Body(...)
    ) -> dict[str, Any]:
        ref = _ref(name, deployment)
        path = str(body.get("path") or "").strip()
        if not path:
            raise HTTPException(status_code=400, detail="path is required")
        as_name = str(body.get("as") or "") or None

        def work(say: Callable[[str], None]) -> dict[str, Any]:
            dep, entry = deployer(ref, on_progress=say)
            ops = ExtensionOps(dep, on_progress=say)
            pushed = asdict(ops.push(path, name=as_name))
            record(entry, "ext-push", ok=True, detail=str(pushed.get("dirname", "")))
            return pushed

        return {"job": jobs.start("ext-push", name, work).id}

    @app.post("/api/hosts/{name}/extensions/apply")
    def ext_apply(
        name: str, deployment: str | None = None, body: dict[str, Any] = Body(default={})
    ) -> dict[str, Any]:
        ref = _ref(name, deployment)
        only = body.get("only")
        restart = bool(body.get("restart", True))

        def work(say: Callable[[str], None]) -> list[dict[str, Any]]:
            dep, entry = deployer(ref, on_progress=say)
            changes = ExtensionOps(dep, on_progress=say).apply(names=only, restart=restart)
            record(entry, "ext-apply", ok=True,
                   detail=f"{sum(1 for c in changes if c.updated)}/{len(changes)} 个 profile 有改动")
            return [
                {"id": c.id, "name": c.name, "updated": c.updated, "stopped": c.stopped,
                 "before": c.before, "after": c.after, "summary": c.summary}
                for c in changes
            ]

        return {"job": jobs.start("ext-apply", name, work).id}

    @app.post("/api/hosts/{name}/extensions/verify")
    def ext_verify(
        name: str, deployment: str | None = None, body: dict[str, Any] = Body(default={})
    ) -> dict[str, Any]:
        ref = _ref(name, deployment)
        launch = bool(body.get("launch", True))
        settle = float(body.get("settle", 6.0))

        def work(say: Callable[[str], None]) -> list[dict[str, Any]]:
            ops = ExtensionOps(deployer(ref)[0], on_progress=say)
            return [
                {"id": r.id, "name": r.name, "running": r.running, "ok": r.ok,
                 "expected": r.expected, "loaded": sorted(r.loaded), "summary": r.summary}
                for r in ops.verify(launch=launch, settle=settle)
            ]

        return {"job": jobs.start("ext-verify", name, work).id}

    # ---------------------------------------------------------------- #
    # job
    # ---------------------------------------------------------------- #

    @app.get("/api/jobs")
    def list_jobs() -> list[dict[str, Any]]:
        return jobs.all()

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: str) -> dict[str, Any]:
        job = jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="no such job")
        return job.public()

    @app.get("/api/jobs/{job_id}/events")
    def job_events(job_id: str, request: Request, after: int = 0) -> StreamingResponse:
        """SSE。部署要几分钟，界面必须能一行一行看到进度。"""
        job = jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="no such job")

        def stream() -> Iterator[str]:
            sent = max(after, int(request.headers.get("last-event-id", "0") or 0))
            while True:
                if not auth.valid(request.headers, request.cookies):
                    yield _sse("auth_expired", {"message": "凭据已失效，请重新登录"})
                    return
                sent = max(sent, job.log_offset)
                lines = job.lines[sent - job.log_offset:]
                for line in lines:
                    sent += 1
                    yield f"id: {sent}\n" + _sse("line", {"text": line, "cursor": sent})
                if job.status != "running":
                    yield _sse("done", job.public())
                    return
                yield ": keepalive\n\n"                     # 别让反代掐掉空闲连接
                time.sleep(0.4)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    setup_viewer(app, deployer)
    return app


def _sse(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"


def _spec_overrides(body: dict[str, Any]) -> dict[str, Any]:
    """只认 DeploySpec 认识的字段，其它一律丢掉。"""
    fields = set(asdict(DeploySpec()))
    return {k: v for k, v in (body.get("spec") or {}).items() if k in fields and v not in (None, "")}


def _version() -> str:
    from ... import __version__

    return __version__


# --------------------------------------------------------------------------- #


def serve(*, host: str = "127.0.0.1", port: int = 8700, token: str | None = None) -> int:
    """起 uvicorn。

    :param host: 监听地址。默认只听本机
    :param port: 监听端口
    :param token: 可选管理员引导口令，首次启动可自动签发
    :returns: 退出码
    :raises DeployError: 没装 fastapi/uvicorn
    """
    _require_fastapi()
    try:
        import uvicorn
    except ModuleNotFoundError as exc:                     # pragma: no cover - 取决于环境
        raise DeployError(
            "uvicorn is missing from this installation; repair it with: pip install sleight"
        ) from exc

    import os
    token = token or os.environ.get("SLEIGHT_UI_TOKEN")
    from ...service.identity import Identity
    identity = Identity()
    if not token and not identity.users():
        import secrets
        token = "sl_" + secrets.token_urlsafe(32)
        identity.ensure_admin(token)
        print("首次管理员 token（仅显示一次，请保存）：" + token)
    app = create_app(token=token, require_auth=True)
    print(f"sleight ui → http://{host}:{port}")
    print("  使用用户 token 登录；API 和 MCP 使用相同凭据。")
    uvicorn.run(app, host=host, port=port, log_level="info")
    return 0
