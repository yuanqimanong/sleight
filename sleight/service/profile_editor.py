"""Edit stopped profiles in place, preserving their identity and browser data."""

import uuid
from contextlib import contextmanager

from ..core import errors
from ..core.types import InstanceStatus
from ..providers.cloakbrowser import CLEAR, ProfileSpec
from ..providers.compat import extension_list
from ..runtime.capabilities import browser_capabilities
from ..runtime.config import Templates, mask_proxy
from ..runtime.native import NativeSupervisor
from .database import storage_write
from .execution import manager_for
from .fleet import Fleet, is_admin
from .identity import current
from .launch_templates import LaunchTemplates

FIELDS = {"name", "notes", "headless", "no_sandbox", "fingerprint_seed", "template_id", "launch_template_id", "extension_paths"}


class ProfileEditor:
    def __init__(self, supervisor):
        self.supervisor, self.db = supervisor, supervisor.db
        self.fleet = Fleet(supervisor)

    @contextmanager
    def source(self, row):
        if row["environment"] == "native":
            native = NativeSupervisor()
            yield native, native.get(row["source_id"])
        else:
            with manager_for(row["environment"]) as manager:
                yield manager, manager.get_profile(row["source_id"])

    def configuration(self, ref, user):
        row = self.fleet.resolve(ref, user)
        with self.source(row) as (driver, source):
            native = row["environment"] == "native"
            caps = browser_capabilities(source["binary"]) if native else {"kind": "cloak", "extensions": "automatic"}
            result = {"ref": ref, "name": row["name"], "notes": row.get("notes", source.get("notes") or ""),
                      "environment": row["environment"], "ephemeral": row["ephemeral"], "kind": caps["kind"],
                      "template_id": row.get("template_id", ""), "launch_template_id": row.get("launch_template_id", ""),
                      "headless": bool(source.get("headless", False)), "no_sandbox": bool(source.get("no_sandbox", False)),
                      "fingerprint_seed": source.get("fingerprint", source.get("fingerprint_seed")),
                      "extensions": caps["extensions"], "proxy_display": mask_proxy(source.get("proxy") or ""),
                      "metadata_only": bool(row.get("external") and not is_admin(user))}
            if not native:
                result["generation"] = driver.capabilities()["generation"]
                if result["generation"] == "modern":
                    result["headless"] = False
            if is_admin(user):
                result["extension_paths"] = list(source.get("extension_paths", [])) if native else extension_list(source)
            return result

    @storage_write
    def update(self, ref, changes, user):
        if not isinstance(changes, dict) or set(changes) - FIELDS:
            raise ValueError("内核、环境、使用方式和实例身份不能修改")
        row = self.fleet.resolve(ref, user)
        if row.get("state") in ("creating", "cleanup_pending", "cleaning"):
            raise errors.Busy("实例正在创建或回收，请稍后重试")
        key = row["environment"] + ":" + row["source_id"]
        with self.db.state("service-sessions") as state:
            if key in state.get("deletions", {}) or any(s["state"] != "released" and
                    (s.get("source_key") == key or s["profile"] == row.get("id")) for s in state["records"].values()):
                raise errors.Busy("请先停止实例，等待会话回收后再编辑")
        if row.get("external") and not is_admin(user) and set(changes) - {"name", "notes"}:
            raise PermissionError("共享固定实例只能修改自己的名称和备注")
        if "extension_paths" in changes and not is_admin(user):
            raise PermissionError("只有管理员可以指定服务器插件目录")
        name = str(changes.get("name", row["name"])).strip()
        if not name or len(name) > 100:
            raise ValueError("请输入 1–100 字符的实例名称")
        notes = str(changes.get("notes", row.get("notes", "")))
        if len(notes) > 1000:
            raise ValueError("备注最多 1000 字符")
        if any(r["environment"] == row["environment"] and r["id"] != row.get("id")
               and r["name"].strip().casefold() == name.casefold() for r in self.db.list("profiles")):
            raise errors.Busy("同一执行环境中已有这个实例名称")
        with self.db.state("profile-requests") as requests:
            if any(r.get("environment") == row["environment"] and r.get("name") == name.casefold()
                   and r["state"] == "creating" and r["id"] != row.get("id") for r in requests["records"].values()):
                raise errors.Busy("同名实例正在创建，请稍后重试")
        patch = {k: v for k, v in changes.items() if k in {"headless", "no_sandbox", "fingerprint_seed", "extension_paths"}}
        updates = {"name": name, "notes": notes}
        if "launch_template_id" in changes:
            launch = LaunchTemplates().get(changes["launch_template_id"])
            kind = "cloak" if row["environment"] != "native" else browser_capabilities(NativeSupervisor().get(row["source_id"])["binary"])["kind"]
            if launch["kind"] != kind:
                raise ValueError("创建模板与实例内核不一致")
            patch = {**{k: v for k, v in launch["config"].items() if k != "binary"}, **patch}
            patch.pop("auto_launch", None)
            updates["launch_template_id"] = launch["id"]
        context_token = current.set(user)
        try:
            if "template_id" in changes:
                template_id = changes["template_id"] or (self.supervisor.identity.user(user.user_id) or {}).get("default_template") or ""
                applied = Templates().apply({"template_id": template_id}) if template_id else {"proxy": "", "extension_paths": []}
                patch = {**{k: applied[k] for k in ("proxy", "extension_paths")}, **patch}
                sid = row.get("proxy_sid") or uuid.uuid4().hex[:10]
                if "{sid}" in patch["proxy"]:
                    patch["proxy"] = patch["proxy"].replace("{sid}", sid)
                    updates["proxy_sid"] = sid
                else:
                    updates["proxy_sid"] = ""
                updates.update(template_id=template_id, proxy_display=mask_proxy(patch["proxy"]))
            for option in ("headless", "no_sandbox"):
                if option in patch and type(patch[option]) is not bool:
                    raise ValueError("启动选项需要布尔值")
            if "fingerprint_seed" in patch:
                seed = patch["fingerprint_seed"]
                if type(seed) is not int or not 1 <= seed <= 2**31 - 1:
                    raise ValueError("指纹种子需要 1–2147483647 的整数")
                updates["fingerprint_seed"] = seed
            with self.source(row) as (driver, source):
                native = row["environment"] == "native"
                stopped = source.get("status") == "stopped" if native else driver.status(row["source_id"]) == InstanceStatus.STOPPED
                if not stopped:
                    raise errors.Busy("请先停止实例再修改配置")
                row = self.fleet.bind(row, user)
                if not native and patch:
                    patch.pop("no_sandbox", None)
                    if "extension_paths" in patch:
                        if not isinstance(patch["extension_paths"], list):
                            raise ValueError("插件目录需要列表")
                        patch["launch_args"] = [a for a in source.get("launch_args", []) if not str(a).startswith(("--load-extension=", "--disable-extensions-except="))]
                    if driver.capabilities()["generation"] == "modern":
                        gpu = str(patch.get("gpu_vendor", "")).lower()
                        if "gpu_vendor" in patch or "gpu_renderer" in patch:
                            patch["gpu_family"] = patch.get("gpu_family") or ("nvidia" if "nvidia" in gpu else "intel" if "intel" in gpu else "auto")
                            patch.pop("gpu_vendor", None)
                            patch.pop("gpu_renderer", None)
                    ProfileSpec(name=name, **patch).validate()
                updated = {**row, **updates, "touched": self.db.now()}
                self.db.put("profiles", row["id"], updated)
                try:
                    if native:
                        driver.update(row["source_id"], {**patch, "name": name, "notes": notes})
                    elif patch:
                        driver.update_profile(row["source_id"], **{k: CLEAR if v == "" else v for k, v in patch.items()})
                except Exception:
                    self.db.put("profiles", row["id"], row)
                    self.fleet.record(row["id"], "edit", "配置更新失败；请重新打开编辑核对实际配置")
                    raise
            with self.db.state("profile-requests") as requests:
                for record in requests["records"].values():
                    if record["id"] == row["id"]:
                        record["name"] = name.casefold()
            self.fleet.record(row["id"], "edit", "实例配置已更新；浏览器数据保留，下次启动生效")
            return updated
        finally:
            current.reset(context_token)
