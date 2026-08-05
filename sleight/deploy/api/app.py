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
from ..inventory import Host, Inventory
from ..ops import ExtensionOps, ProfileOps, extension_paths
from ..spec import DEFAULT_IMAGE, DeploySpec

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

    def deployer(
        name: str,
        *,
        on_progress: Callable[[str], None] | None = None,
        **overrides: Any,
    ) -> Deployer:
        host = _host(name)
        spec = host.spec(**{k: v for k, v in overrides.items() if v is not None})
        return Deployer(spec, host.runner(), sudo=host.sudo, on_progress=on_progress)

    def _host(name: str) -> Host:
        if name == "local":
            inv = Inventory.load()
            return inv.hosts.get("local") or Host(name="local")
        try:
            return Inventory.load().get(name)
        except DeployError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

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
        }

    # ---------------------------------------------------------------- #
    # 主机
    # ---------------------------------------------------------------- #

    @app.get("/api/hosts")
    def list_hosts() -> list[dict[str, Any]]:
        inv = Inventory.load()
        out = [
            {
                "name": name, "ssh": h.ssh, "port": h.port, "identity": h.identity,
                "sudo": h.sudo, "local": h.local, "deploy": h.deploy,
                "spec": asdict(h.spec()),
            }
            for name, h in sorted(inv.hosts.items())
        ]
        if "local" not in inv.hosts:
            # 本机永远可选：装了 docker 就能一键部署，不必先配 hosts.toml
            out.insert(0, {
                "name": "local", "ssh": "", "port": None, "identity": "", "sudo": False,
                "local": True, "deploy": {}, "spec": asdict(DeploySpec()), "implicit": True,
            })
        return out

    @app.post("/api/hosts")
    def add_host(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        name = str(body.get("name") or "").strip()
        if not name:
            raise HTTPException(status_code=400, detail="name is required")
        deploy = {k: v for k, v in (body.get("deploy") or {}).items() if v not in (None, "")}
        wrap(lambda: DeploySpec.from_dict(deploy).validate())
        inv = Inventory.load()
        inv.add(Host(
            name=name,
            ssh=str(body.get("ssh") or ""),
            port=int(body["port"]) if body.get("port") else None,
            identity=str(body.get("identity") or ""),
            sudo=bool(body.get("sudo")),
            strict_host_key="accept-new" if body.get("accept_new") else "",
            deploy=deploy,
        ))
        inv.save()
        return {"ok": True, "path": str(inv.path)}

    @app.delete("/api/hosts/{name}")
    def remove_host(name: str) -> dict[str, Any]:
        inv = Inventory.load()
        wrap(lambda: inv.remove(name))
        inv.save()
        return {"ok": True}

    # ---------------------------------------------------------------- #
    # 部署
    # ---------------------------------------------------------------- #

    @app.post("/api/hosts/{name}/preflight")
    def preflight(name: str, body: dict[str, Any] = Body(default={})) -> dict[str, Any]:
        dep = deployer(name, **_spec_overrides(body))
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
    def status(name: str) -> dict[str, Any]:
        return wrap(deployer(name).status)

    @app.get("/api/hosts/{name}/logs")
    def logs(name: str, tail: int = 200) -> dict[str, Any]:
        return {"text": wrap(lambda: deployer(name).logs(tail=tail))}

    @app.post("/api/hosts/{name}/deploy")
    def deploy(name: str, body: dict[str, Any] = Body(default={})) -> dict[str, Any]:
        overrides = _spec_overrides(body)
        force = bool(body.get("force"))

        def work(say: Callable[[str], None]) -> dict[str, Any]:
            dep = deployer(name, on_progress=say, **overrides)
            result = dep.apply(force=force)
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
    def upgrade(name: str, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        image = str(body.get("image") or "").strip()
        if not image:
            raise HTTPException(status_code=400, detail="image is required")
        backup = bool(body.get("backup", True))

        def work(say: Callable[[str], None]) -> dict[str, Any]:
            dep = deployer(name, on_progress=say)
            result = dep.upgrade(image, backup=backup)
            return {"summary": result.summary, "status": result.status}

        return {"job": jobs.start("upgrade", name, work).id}

    @app.post("/api/hosts/{name}/backup")
    def backup(name: str) -> dict[str, Any]:
        def work(say: Callable[[str], None]) -> dict[str, Any]:
            dep = deployer(name, on_progress=say)
            return {"archive": dep.backup()}

        return {"job": jobs.start("backup", name, work).id}

    # ---------------------------------------------------------------- #
    # profile
    # ---------------------------------------------------------------- #

    @app.get("/api/hosts/{name}/profiles")
    def profiles(name: str) -> list[dict[str, Any]]:
        raw = wrap(ProfileOps(deployer(name)).list)
        for p in raw:
            p["extension_paths"] = extension_paths([str(a) for a in (p.get("launch_args") or [])])
        return raw

    @app.post("/api/hosts/{name}/profiles/{pid}/{action}")
    def profile_action(name: str, pid: str, action: str) -> dict[str, Any]:
        ops = ProfileOps(deployer(name))
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
    def extensions(name: str) -> list[dict[str, Any]]:
        ops = ExtensionOps(deployer(name))
        return [asdict(e) for e in wrap(ops.list_installed)]

    @app.get("/api/hosts/{name}/extensions/drift")
    def drift(name: str) -> dict[str, Any]:
        return wrap(ExtensionOps(deployer(name)).drift)

    @app.post("/api/hosts/{name}/extensions/push")
    def push(name: str, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        path = str(body.get("path") or "").strip()
        if not path:
            raise HTTPException(status_code=400, detail="path is required")
        as_name = str(body.get("as") or "") or None

        def work(say: Callable[[str], None]) -> dict[str, Any]:
            dep = deployer(name, on_progress=say)
            ops = ExtensionOps(dep, on_progress=say)
            return asdict(ops.push(path, name=as_name))

        return {"job": jobs.start("ext-push", name, work).id}

    @app.post("/api/hosts/{name}/extensions/apply")
    def ext_apply(name: str, body: dict[str, Any] = Body(default={})) -> dict[str, Any]:
        only = body.get("only") or None
        restart = bool(body.get("restart", True))

        def work(say: Callable[[str], None]) -> list[dict[str, Any]]:
            ops = ExtensionOps(deployer(name), on_progress=say)
            return [
                {"id": c.id, "name": c.name, "updated": c.updated, "stopped": c.stopped,
                 "before": c.before, "after": c.after, "summary": c.summary}
                for c in ops.apply(names=only, restart=restart)
            ]

        return {"job": jobs.start("ext-apply", name, work).id}

    @app.post("/api/hosts/{name}/extensions/verify")
    def ext_verify(name: str, body: dict[str, Any] = Body(default={})) -> dict[str, Any]:
        launch = bool(body.get("launch", True))
        settle = float(body.get("settle", 6.0))

        def work(say: Callable[[str], None]) -> list[dict[str, Any]]:
            ops = ExtensionOps(deployer(name), on_progress=say)
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
