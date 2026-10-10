"""Console operations use the same ownership, leases and actors as SDK/MCP."""

import uuid

from fastapi import Body, HTTPException, Request

from ..runtime.capabilities import browser_capabilities
from ..runtime.native import discover_browsers
from .fleet import Fleet, is_admin
from .launch_templates import LaunchTemplates
from .monitoring import Monitor
from .profile_editor import ProfileEditor
from .registry import manager_images
from .storage_config import StorageSettings


def setup_console(app, user, admin, invoke):
    fleet = Fleet(app.state.supervisor)
    monitor = Monitor()
    app.state.monitor = monitor

    @app.get("/api/v1/fleet")
    def catalogue(request: Request):
        return invoke(lambda: fleet.catalogue(user(request)))

    @app.get("/api/v1/kernels")
    def kernels(request: Request):
        import hashlib
        principal = user(request)
        rows = discover_browsers()
        rows = [{**r, "id": hashlib.sha256(r["binary"].encode()).hexdigest()[:16]} for r in rows]
        return rows if principal.role == "admin" and "manage" in principal.scopes else [{k: v for k, v in r.items() if k != "binary"} for r in rows]

    @app.post("/api/v1/kernels/check")
    def kernel_check(request: Request, body: dict = Body(...)):
        admin(request)
        from ..providers.local import _resolve_binary
        return invoke(lambda: {"binary": _resolve_binary(body.get("binary", "")), **browser_capabilities(_resolve_binary(body.get("binary", "")))})

    @app.post("/api/v1/fleet/create")
    def create(request: Request, body: dict = Body(...)):
        principal = user(request)
        if "execute" not in principal.scopes:
            raise HTTPException(403, "需要浏览器执行权限")
        row = invoke(lambda: app.state.supervisor.controller.create(body, principal, request.headers.get("idempotency-key") or uuid.uuid4().hex))
        fleet.record(row["id"], "create", "实例配置已创建")
        return row

    @app.get("/api/v1/fleet/name")
    def name_available(request: Request, name: str, environment: str = "native", exclude: str = ""):
        data = invoke(lambda: fleet.catalogue(user(request)))
        return {"available": not any(r["ref"] != exclude and r["environment"] == environment and r["name"].strip().casefold() == name.strip().casefold() for r in data["instances"])}

    @app.get("/api/v1/fleet/configuration")
    def configuration(request: Request, ref: str):
        return invoke(lambda: ProfileEditor(app.state.supervisor).configuration(ref, user(request)))

    @app.post("/api/v1/fleet/edit")
    def edit(request: Request, body: dict = Body(...)):
        principal = user(request)
        if "execute" not in principal.scopes:
            raise HTTPException(403, "需要浏览器执行权限")
        return invoke(lambda: ProfileEditor(app.state.supervisor).update(str(body.get("ref", "")), body.get("changes", {}), principal))

    @app.post("/api/v1/fleet/action")
    def action(request: Request, body: dict = Body(...)):
        principal = user(request)
        ref, operation = str(body.get("ref", "")), str(body.get("action", ""))
        def work():
            row = fleet.resolve(ref, principal)
            try:
                return fleet.action(ref, operation, principal)
            except Exception as exc:
                fleet.record(row.get("id", ""), operation, "操作失败：" + type(exc).__name__ + "；请检查实例状态后重试")
                raise
        return invoke(work)

    @app.get("/api/v1/fleet/inspect")
    def inspect(request: Request, ref: str, target: str = ""):
        return invoke(lambda: fleet.inspect(ref, user(request), target=target))

    @app.post("/api/v1/fleet/navigate")
    def navigate(request: Request, body: dict = Body(...)):
        return invoke(lambda: fleet.inspect(str(body.get("ref", "")), user(request), target=str(body.get("target", "")),
                                           url=str(body.get("url", "")), mode=str(body.get("mode", "current"))))

    @app.get("/api/v1/fleet/logs")
    def logs(request: Request, ref: str):
        return invoke(lambda: fleet.logs(ref, user(request)))

    @app.post("/api/v1/fleet/delete")
    def remove(request: Request, body: dict = Body(...)):
        return invoke(lambda: fleet.remove(str(body.get("ref", "")), user(request), str(body.get("confirm", ""))))

    @app.get("/api/v1/launch-templates")
    def launch_templates(request: Request):
        user(request)
        return LaunchTemplates().list()

    @app.get("/api/v1/fleet/capabilities")
    def manager_capabilities(request: Request, environment: str):
        principal = user(request)
        default = app.state.supervisor.identity.user(principal.user_id) or {}
        if not is_admin(principal) and environment not in [default.get("default_environment"), *default.get("environments", [])]:
            raise HTTPException(403, "此用户未获授权使用目标环境")
        from .execution import manager_for
        def read():
            with manager_for(environment) as manager:
                return manager.capabilities()
        return invoke(read)

    @app.post("/api/v1/launch-templates")
    def save_launch(request: Request, body: dict = Body(...)):
        admin(request)
        return invoke(lambda: LaunchTemplates().save(body))

    @app.delete("/api/v1/launch-templates/{pid}")
    def delete_launch(request: Request, pid: str):
        admin(request)
        return invoke(lambda: LaunchTemplates().delete(pid))

    @app.get("/api/v1/metrics")
    def metrics(request: Request):
        user(request)
        return monitor.metrics()

    @app.get("/api/v1/settings/alerts")
    def alerts(request: Request):
        admin(request)
        return monitor.settings()

    @app.post("/api/v1/settings/alerts")
    def save_alerts(request: Request, body: dict = Body(...)):
        admin(request)
        return invoke(lambda: monitor.save(body))

    @app.get("/api/v1/settings/storage")
    def storage(request: Request):
        admin(request)
        return invoke(lambda: StorageSettings(app.state.supervisor.db).status())

    @app.post("/api/v1/settings/storage/test")
    def test_storage(request: Request, body: dict = Body(...)):
        admin(request)
        return invoke(lambda: StorageSettings(app.state.supervisor.db).test(body))

    @app.post("/api/v1/settings/storage/migrate")
    def migrate_storage(request: Request, body: dict = Body(...)):
        admin(request)
        return invoke(lambda: StorageSettings(app.state.supervisor.db).migrate(body))

    @app.get("/api/v1/manager-images")
    def images(request: Request, refresh: bool = False):
        admin(request)
        return invoke(lambda: manager_images(refresh=refresh))
