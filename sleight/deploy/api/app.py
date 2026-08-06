"""FastAPI 应用：用界面做部署和运维。

**这个界面能在目标机上执行 ssh 和 docker 命令**，等于一个远程执行入口。所以：

* 默认只监听 ``127.0.0.1``；
* 绑到别的地址时**必须**给 ``--token``，否则 :func:`serve` 直接拒绝启动；
* 它不存任何 Manager token —— 要用时现从目标机的 ``.env`` 读。

拉镜像动辄几分钟，所以部署、下发、验证这些长动作都返回一个 job id，进度用 SSE
（``/api/jobs/{id}/events``）推。界面因此是流式的，不是一个转圈等超时的 POST。
"""

from __future__ import annotations

import ipaddress
import json
import logging
import secrets
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ...core.errors import SleightError
from ..engine import Deployer
from ..errors import DeployError
from ..ops import ExtensionOps, ProfileOps, extension_paths
from ..presets import DEPLOY_TEMPLATES, FIELD_HELP, PROFILE_PRESETS, profile_spec_from
from ..spec import DEFAULT_IMAGE, DeploySpec
from ..store import Deployment, Host, Store

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
    from fastapi import Body, Depends, FastAPI, HTTPException, Query, Request
    from fastapi.responses import HTMLResponse, StreamingResponse

    HAS_FASTAPI = True
except ModuleNotFoundError:                                # pragma: no cover - 取决于环境
    HAS_FASTAPI = False
    Body = Depends = FastAPI = HTTPException = Query = Request = None      # type: ignore[assignment]
    HTMLResponse = StreamingResponse = None                                # type: ignore[assignment]


def _require_fastapi() -> None:
    if not HAS_FASTAPI:                                    # pragma: no cover - 取决于环境
        raise DeployError(
            'the web UI needs FastAPI: pip install "sleight[ui]"  '
            "(the CLI and the Python API work without it)"
        )


# --------------------------------------------------------------------------- #
# job
# --------------------------------------------------------------------------- #


@dataclass
class Job:
    """一个后台动作。"""

    id: str
    kind: str
    host: str
    status: str = "running"                # running | ok | error
    lines: list[str] = field(default_factory=list)
    error: str = ""
    result: Any = None
    started: float = field(default_factory=time.monotonic)
    finished: float = 0.0

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id, "kind": self.kind, "host": self.host, "status": self.status,
            "lines": list(self.lines), "error": self.error, "result": self.result,
        }


class Jobs:
    """进程内的 job 表。

    刻意不做持久化 —— 界面进程重启后一个"还在跑"的假状态比没有更糟。
    """

    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    def start(self, kind: str, host: str, work: Callable[[Callable[[str], None]], Any]) -> Job:
        job = Job(id=uuid.uuid4().hex[:12], kind=kind, host=host)
        with self._lock:
            self._reap()
            self._jobs[job.id] = job

        def say(message: str) -> None:
            with self._lock:
                job.lines.append(message)

        def run() -> None:
            try:
                result = work(say)
            except BaseException as exc:                    # 线程里的异常必须落到 job 上
                log.exception("job %s (%s) failed", job.id, kind)
                with self._lock:
                    job.status = "error"
                    job.error = f"{type(exc).__name__}: {exc}"
            else:
                with self._lock:
                    job.status = "ok"
                    job.result = result
            finally:
                job.finished = time.monotonic()

        threading.Thread(target=run, name=f"sleight-job-{job.id}", daemon=True).start()
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def all(self) -> list[dict[str, Any]]:
        with self._lock:
            return [j.public() for j in sorted(self._jobs.values(), key=lambda j: -j.started)]

    def _reap(self) -> None:
        cutoff = time.monotonic() - JOB_TTL
        for key in [k for k, j in self._jobs.items() if j.finished and j.finished < cutoff]:
            del self._jobs[key]


# --------------------------------------------------------------------------- #
# 应用
# --------------------------------------------------------------------------- #


def create_app(*, token: str | None = None) -> Any:
    """建 FastAPI 应用。

    :param token: 访问口令。给了就每个请求都要带 ``X-Sleight-Token`` 头或 ``?token=``
        （``EventSource`` 设不了自定义头，所以查询参数也收）
    :returns: ``fastapi.FastAPI`` 实例
    :raises DeployError: 没装 fastapi
    """
    _require_fastapi()
    jobs = Jobs()

    def guard(request: Request, tok: str | None = Query(None, alias="token")) -> None:
        if not token:
            return
        given = request.headers.get("X-Sleight-Token") or tok or ""
        # 定时比较：这个口令是唯一的门
        if not secrets.compare_digest(given, token):
            raise HTTPException(status_code=401, detail="bad or missing UI token")

    app = FastAPI(
        title="sleight deploy",
        description="部署与运维 CloakBrowser Manager",
        version=_version(),
        dependencies=[Depends(guard)],
    )

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

    def wrap(fn: Callable[[], Any]) -> Any:
        """把库里的异常翻成 HTTP 错误，而不是 500 + 一页 traceback。"""
        try:
            return fn()
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
            step("内存", ["sh", "-c", "free -g | awk 'NR==2{print $2\" GB\"}'"])
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
                "deployments": [
                    Deployment(host="local", name="default", spec=DeploySpec()).to_dict()
                ],
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
        wrap(lambda: db.put_deployment(host, name, DeploySpec.from_dict(spec_fields)))
        return {"ok": True, "ref": f"{host}/{name}"}

    @app.delete("/api/deployments/{host}/{name}")
    def remove_deployment(host: str, name: str) -> dict[str, Any]:
        wrap(lambda: store().delete_deployment(host, name))
        return {"ok": True}

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
        return {"token": value, "url": dep.spec.local_url, "env_path": dep.spec.env_path}

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
            dep.destroy(purge_data=purge)
            record(entry, "destroy", ok=True,
                   detail="连 data/ 一起删" if purge else "保留 data/")
            return {"purged": purge}

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
            p["extension_paths"] = extension_paths([str(a) for a in (p.get("launch_args") or [])])
        return raw

    @app.post("/api/hosts/{name}/profiles")
    def create_profile(
        name: str, deployment: str | None = None, body: dict[str, Any] = Body(...)
    ) -> dict[str, Any]:
        """按身份模板建一个实例。**幂等**：同名的会被更新而不是再建一个。

        ``auto_launch`` 一律 False —— 建的时候不该顺手拉起一个浏览器占着内存，
        要用时 ``lease()`` 会自己拉。
        """
        profile_name = str(body.get("name") or "").strip()
        if not profile_name:
            raise HTTPException(status_code=400, detail="实例得有个名字")
        overrides: dict[str, Any] = {
            "proxy": body.get("proxy"),
            "geoip": bool(body.get("geoip")),
            "headless": bool(body.get("headless")),
            "notes": body.get("notes"),
            "tags": tuple(t.strip() for t in str(body.get("tags") or "").split(",") if t.strip()),
        }
        for key in ("screen_width", "screen_height", "fingerprint_seed"):
            if body.get(key):
                overrides[key] = int(body[key])
        spec = wrap(lambda: profile_spec_from(
            str(body.get("preset") or "windows_us"), profile_name, **overrides
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
        only = body.get("only") or None
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
    def job_events(job_id: str) -> StreamingResponse:
        """SSE。部署要几分钟，界面必须能一行一行看到进度。"""
        job = jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="no such job")

        def stream() -> Iterator[str]:
            sent = 0
            while True:
                lines = job.lines[sent:]
                sent += len(lines)
                for line in lines:
                    yield _sse("line", {"text": line})
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
    :param token: 访问口令。绑非回环地址时必填
    :returns: 退出码
    :raises DeployError: 绑了对外地址却没给 token，或没装 fastapi/uvicorn
    """
    _require_fastapi()
    try:
        import uvicorn
    except ModuleNotFoundError as exc:                     # pragma: no cover - 取决于环境
        raise DeployError('the web UI needs uvicorn: pip install "sleight[ui]"') from exc

    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = False
    if not loopback and not token:
        raise DeployError(
            f"refusing to listen on {host} without --token: this UI runs ssh and docker "
            "commands on your hosts, so an open port is a remote execution surface"
        )

    app = create_app(token=token)
    print(f"sleight ui → http://{host}:{port}")
    if token:
        print(f"  口令 {token}  （界面会把它记在 localStorage）")
        print(f"  直接带上：http://{host}:{port}/?token={token}")
    else:
        print("  只监听本机，没有口令。要对外开就必须 --token。")
    uvicorn.run(app, host=host, port=port, log_level="info")
    return 0
