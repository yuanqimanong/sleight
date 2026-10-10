"""Typed creation templates; proxy credentials remain in separate templates."""

import uuid
from dataclasses import fields

from ..providers.cloakbrowser import ProfileSpec
from .database import database

BASE = {"timezone": "America/New_York", "locale": "en-US", "platform": "windows", "headless": False,
        "humanize": False, "human_preset": "default", "geoip": False, "clipboard_sync": True}
BUILTINS = [
    {"id": "builtin-native", "name": "本机原生", "kind": "native", "config": {"headless": False}},
    {"id": "builtin-fingerprint", "name": "fingerprint-chromium", "kind": "fingerprint", "config": {"headless": False}},
    *[{"id": "builtin-cloak-" + str(i), "name": "Win-US-0" + str(i), "kind": "cloak",
       "config": {**BASE, "fingerprint_seed": seed, "screen_width": width, "screen_height": height,
                  "color_scheme": color, "gpu_vendor": vendor, "gpu_renderer": renderer},
       "note": "新版 Manager 使用 GPU 家族，具体型号由内核决定；AMD 配置保留在旧版模板，现代模式使用 auto。"}
      for i, seed, width, height, color, vendor, renderer in [
          (0, 89757, 1920, 1080, "light", "Google Inc. (NVIDIA)", "ANGLE (NVIDIA, NVIDIA GeForce RTX 3070 (0x00002484) Direct3D11 vs_5_0 ps_5_0, D3D11)"),
          (1, 9527, 1920, 1080, "light", "Google Inc. (NVIDIA)", "ANGLE (NVIDIA, NVIDIA GeForce RTX 4070 (0x00002786) Direct3D11 vs_5_0 ps_5_0, D3D11)"),
          (2, 78335, 2560, 1440, "dark", "Google Inc. (AMD)", "ANGLE (AMD, AMD Radeon RX 6800 XT (0x000073BF) Direct3D11 vs_5_0 ps_5_0, D3D11)")]],
]
FIELDS = {f.name for f in fields(ProfileSpec)} - {"name", "proxy", "extension_paths", "launch_args", "tags", "auto_launch"}


class LaunchTemplates:
    def __init__(self):
        self.db = database()

    def list(self):
        return [*[{**r, "builtin": True} for r in BUILTINS], *self.db.list("launch-templates")]

    def get(self, pid):
        value = next((r for r in self.list() if r["id"] == pid), None)
        if not value:
            raise ValueError("创建模板已删除，请重新选择")
        return value

    def save(self, body):
        name = str(body.get("name", "")).strip()
        kind = body.get("kind", "cloak")
        if not name or len(name) > 100 or kind not in ("native", "fingerprint", "cloak"):
            raise ValueError("请输入有效模板名称和创建类型")
        config = body.get("config", {})
        allowed = FIELDS if kind == "cloak" else {"binary", "headless", "no_sandbox", "fingerprint_seed"} if kind == "fingerprint" else {"binary", "headless", "no_sandbox"}
        if not isinstance(config, dict) or set(config) - allowed:
            raise ValueError("模板包含当前内核不支持的字段；代理和插件请在专用模板中配置")
        if kind == "cloak":
            ProfileSpec(name=name, **{k: v for k, v in config.items() if v not in (None, "")}).validate()
        pid = str(body.get("id") or uuid.uuid4().hex)
        if pid.startswith("builtin-"):
            raise ValueError("默认模板不能覆盖，请另存为自定义模板")
        row = {"id": pid, "name": name, "kind": kind, "config": config, "note": str(body.get("note", ""))[:1000]}
        self.db.put("launch-templates", pid, row)
        return row

    def delete(self, pid):
        if pid.startswith("builtin-"):
            raise ValueError("默认模板不能删除")
        self.db.delete("launch-templates", pid)
