"""Native deployment routes and the independent Manager garbage collector."""

import asyncio
import logging
from contextlib import asynccontextmanager, suppress

from fastapi import Body, HTTPException, Query

from ...core.errors import Busy, SleightError
from ...runtime.config import Templates, install_browser
from ...runtime.native import NativeSupervisor, discover_browsers, platform_info
from ..engine import Deployer
from ..store import Store

log = logging.getLogger("sleight.collector")


def sweep_managers():
    db = Store()
    for entry in db.deployments():
        try:
            host = db.require_host(entry.host)
            with host.runner() as runner:
                deployer = Deployer(entry.spec, runner, sudo=host.sudo)
                if not deployer.existing_token():
                    continue
                with deployer.connect() as manager:
                    manager.governor.collect(manager)
        except Exception as exc:
            log.warning("Collector %s failed: %s", entry.ref, type(exc).__name__)


def setup_runtime(app, jobs, *, enabled=True):
    native = NativeSupervisor()
    templates = Templates()
    app.state.native = native
    app.state.templates = templates

    @asynccontextmanager
    async def lifespan(_app):
        stop = asyncio.Event()

        async def collector():
            while not stop.is_set():
                try:
                    if not app.state.supervisor.db.storage_pending:
                        await asyncio.to_thread(sweep_managers)
                        await asyncio.to_thread(app.state.supervisor.sweep)
                except Exception as exc:
                    log.warning("Collector unavailable (%s); retrying", type(exc).__name__)
                with suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=15)

        task = asyncio.create_task(collector()) if enabled else None
        async def monitoring():
            while not stop.is_set():
                try:
                    if not app.state.supervisor.db.storage_pending:
                        await asyncio.to_thread(app.state.monitor.sample)
                except Exception as exc:
                    log.warning("Resource monitor unavailable (%s)", type(exc).__name__)
                with suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=5)
        monitor_task = asyncio.create_task(monitoring()) if enabled else None
        try:
            async with app.state.mcp_manager.run():
                yield
        finally:
            stop.set()
            if task:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            if monitor_task:
                monitor_task.cancel()
                await asyncio.gather(monitor_task, return_exceptions=True)
            await asyncio.to_thread(app.state.supervisor.shutdown)
            await asyncio.to_thread(native.close)
            from ...runtime.governor import close_governors
            await asyncio.to_thread(close_governors)

    app.router.lifespan_context = lifespan

    def call(work):
        try:
            return work()
        except Busy as exc:
            raise HTTPException(409, str(exc)) from exc
        except (ValueError, SleightError) as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.get("/api/runtime/platform")
    def platform_route():
        return {**platform_info(), "browsers": discover_browsers()}

    @app.get("/api/runtime/docker")
    def docker_route():
        from ..runner import LocalRunner
        try:
            with LocalRunner() as runner:
                docker = runner.run(["docker", "info", "--format", "{{.OSType}}"], timeout=8)
                compose = runner.run(["docker", "compose", "version", "--short"], timeout=8)
            return {"ok": docker.ok and docker.text.strip() == "linux" and compose.ok,
                    "os": docker.text.strip() if docker.ok else "", "compose": compose.text.strip() if compose.ok else "",
                    "message": "Linux Docker 和 Compose 已就绪" if docker.ok and docker.text.strip() == "linux" and compose.ok else "请启动 Docker Desktop 并切换 Linux containers，或安装 Linux Docker / Compose"}
        except (OSError, SleightError):
            return {"ok": False, "message": "未检测到 Docker，请先安装并启动 Docker Desktop 或 Linux Docker"}

    @app.get("/api/config/templates")
    def list_templates():
        return templates.list()

    @app.post("/api/config/templates")
    def save_template(body: dict = Body(...)):
        return call(lambda: templates.save(body))

    @app.delete("/api/config/templates/{pid}")
    def delete_template(pid: str):
        call(lambda: templates.delete(pid))
        return {"ok": True}

    @app.post("/api/runtime/install")
    def install_route(body: dict = Body(...)):
        return {"job": jobs.start("browser-install", "local",
                                  lambda say: install_browser(body.get("name", ""), say)).id}

    @app.get("/api/runtime/profiles")
    def list_native():
        return native.list()

    @app.post("/api/runtime/profiles")
    def create_native(body: dict = Body(...)):
        return call(lambda: native.create(templates.apply(body)))

    @app.get("/api/runtime/profiles/{pid}/preview")
    def preview_native(pid: str, target: str = ""):
        return call(lambda: native.preview(pid, target))

    @app.post("/api/runtime/profiles/{pid}/{action}")
    def native_action(pid: str, action: str):
        if action not in ("start", "stop"):
            raise HTTPException(400, "unknown action")
        return {"job": jobs.start("native-" + action, "local",
                                  lambda say: getattr(native, action)(pid)).id}

    @app.delete("/api/runtime/profiles/{pid}")
    def delete_native(pid: str, confirm: str = Query("")):
        call(lambda: native.delete(pid, confirm))
        return {"ok": True}
