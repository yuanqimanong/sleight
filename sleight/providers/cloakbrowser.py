"""CloakBrowser Manager provider + Profile 组装。

组装**不需要写 YAML、不需要落盘**：``POST /api/profiles`` 直接吃 JSON body，
``ProfileCreate`` 只有 ``name`` 必填、其余 21 个字段可选。
"""

from __future__ import annotations

import logging
import os
import warnings
from dataclasses import dataclass, fields, replace
from typing import Any, Literal

from ..core.errors import InstanceError, NotFound
from ..core.types import InstanceInfo, InstanceStatus
from .base import HTTPProvider

log = logging.getLogger("sleight.cloakbrowser")

__all__ = ["CloakBrowserManager", "ProfileSpec"]

TOKEN_ENV = "SLEIGHT_CLOAK_TOKEN"
API = "/api/profiles"

Platform = Literal["windows", "macos", "linux"]

#: 常见分辨率。不在表里只是警告 —— 罕见分辨率本身就是熵，不一定是错
COMMON_RESOLUTIONS = frozenset(
    {(1920, 1080), (2560, 1440), (1536, 864), (1366, 768), (1440, 900), (3840, 2160), (1680, 1050)}
)

_GPU = {
    "windows-nvidia": (
        "Google Inc. (NVIDIA)",
        "ANGLE (NVIDIA, NVIDIA GeForce RTX 3070 (0x00002484) Direct3D11 vs_5_0 ps_5_0, D3D11)",
    ),
    "windows-intel": (
        "Google Inc. (Intel)",
        "ANGLE (Intel, Intel(R) UHD Graphics 630 (0x00003E9B) Direct3D11 vs_5_0 ps_5_0, D3D11)",
    ),
    "macos-apple": (
        "Google Inc. (Apple)",
        "ANGLE (Apple, ANGLE Metal Renderer: Apple M2, Unspecified Version)",
    ),
    "linux-mesa": (
        "Google Inc. (Mesa)",
        "ANGLE (Mesa, llvmpipe (LLVM 15.0.7, 256 bits), OpenGL 4.5)",
    ),
}


# --------------------------------------------------------------------------- #
# ProfileSpec
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ProfileSpec:
    """一个 CloakBrowser profile 的完整描述，字段对齐 Manager 的 ``ProfileCreate``。

    只有 ``name`` 必填。``None`` 的字段不会下发，由 Manager 用它自己的默认值。

    直接构造**不做校验**；:meth:`validate` 会在
    :meth:`CloakBrowserManager.create_profile` / :meth:`~CloakBrowserManager.ensure_profile`
    里被调用，几个预设工厂则当场校验。
    """

    name: str
    # 网络
    proxy: str | None = None                    # socks5://user:pass@host:port
    geoip: bool = False                         # 由代理出口 IP 推导时区/语言
    # 身份
    timezone: str | None = None
    locale: str | None = None
    platform: Platform = "windows"
    user_agent: str | None = None               # None = 浏览器自带
    fingerprint_seed: int | None = None         # 固定则指纹可复现
    # 硬件
    screen_width: int = 1920
    screen_height: int = 1080
    gpu_vendor: str | None = None
    gpu_renderer: str | None = None
    hardware_concurrency: int | None = None
    # 行为
    headless: bool = False
    color_scheme: Literal["light", "dark"] | None = None
    clipboard_sync: bool = True
    auto_launch: bool = False                   # 默认不自动起，交给 ensure_ready
    launch_args: tuple[str, ...] = ()
    humanize: bool = False                      # 浏览器侧拟人开关，外部 CDP 拿不到
    human_preset: str = "default"
    # 元信息
    tags: tuple[str, ...] = ()
    notes: str | None = None

    # ---------------------------- 预设 -------------------------------- #

    @classmethod
    def windows_us(cls, name: str, **kw: Any) -> ProfileSpec:
        """Windows + ``America/New_York`` + ``en-US`` + NVIDIA RTX 3070 (D3D11)。

        :param name: profile 名，在 Manager 内唯一
        :param kw: 覆盖任意字段，例如 ``proxy=`` / ``tags=`` / ``fingerprint_seed=``
        :raises ValueError: 覆盖出了自相矛盾的组合
        """
        return cls._preset(name, "windows", "America/New_York", "en-US", "windows-nvidia", kw)

    @classmethod
    def windows_hk(cls, name: str, **kw: Any) -> ProfileSpec:
        """Windows + ``Asia/Hong_Kong`` + ``zh-HK`` + Intel UHD 630。参数同
        :meth:`windows_us`。"""
        return cls._preset(name, "windows", "Asia/Hong_Kong", "zh-HK", "windows-intel", kw)

    @classmethod
    def macos_us(cls, name: str, **kw: Any) -> ProfileSpec:
        """macOS + ``America/Los_Angeles`` + ``en-US`` + Apple M2 Metal。参数同
        :meth:`windows_us`。"""
        return cls._preset(name, "macos", "America/Los_Angeles", "en-US", "macos-apple", kw)

    @classmethod
    def linux_us(cls, name: str, **kw: Any) -> ProfileSpec:
        """Linux + ``America/New_York`` + ``en-US`` + Mesa llvmpipe。参数同
        :meth:`windows_us`。"""
        return cls._preset(name, "linux", "America/New_York", "en-US", "linux-mesa", kw)

    @classmethod
    def _preset(
        cls, name: str, platform: Platform, tz: str, locale: str, gpu: str, kw: dict[str, Any]
    ) -> ProfileSpec:
        """预设的价值就在这：**保证指纹自洽**。

        平台、时区、语言、GPU 串必须是同一台机器上可能出现的组合。
        ``platform="windows"`` 配一串 Apple Metal 的 renderer 是一眼就会被标记的矛盾。
        """
        vendor, renderer = _GPU[gpu]
        defaults: dict[str, Any] = {
            "platform": platform,
            "gpu_vendor": vendor,
            "gpu_renderer": renderer,
        }
        # geoip=True 时不要预填时区/语言，让出口 IP 说了算
        if not kw.get("geoip"):
            defaults["timezone"] = tz
            defaults["locale"] = locale
        spec = cls(name=name, **{**defaults, **kw})
        spec.validate()
        return spec

    def replace(self, **kw: Any) -> ProfileSpec:
        """派生一个改了若干字段的新 spec，原对象不变。**不做校验** ——
        下发时才校验。

        :param kw: 字段名 → 新值
        """
        return replace(self, **kw)

    # ---------------------------- 校验 -------------------------------- #

    def validate(self) -> None:
        """拦截自相矛盾的组合 —— 这些是一眼就会被反检测标记的。

        检查项：GPU 串与平台是否匹配、``geoip`` 与手写时区/语言是否打架、
        ``geoip`` 有没有配代理、UA 声明的系统与 ``platform`` 是否一致。

        :raises ValueError: 存在硬冲突，消息里逐条列出
        :raises UserWarning: 分辨率罕见 —— 只警告不报错，罕见本身就是熵
        """
        problems: list[str] = []
        r = (self.gpu_renderer or "") + " " + (self.gpu_vendor or "")
        rl = r.lower()

        if self.platform == "windows" and ("apple" in rl or "metal" in rl):
            problems.append("platform='windows' but the GPU string is an Apple/Metal renderer")
        if self.platform == "macos" and ("direct3d" in rl or "d3d11" in rl):
            problems.append("platform='macos' but the GPU string is a Direct3D renderer")
        if self.platform == "linux" and ("direct3d" in rl or "metal" in rl):
            problems.append("platform='linux' but the GPU string is a Direct3D/Metal renderer")

        if self.geoip and (self.timezone or self.locale):
            problems.append(
                "geoip=True derives timezone/locale from the proxy exit IP; "
                "setting them by hand fights it — pick one"
            )
        if self.geoip and not self.proxy:
            problems.append("geoip=True without a proxy has nothing to derive from")

        if ua := self.user_agent:
            ual = ua.lower()
            declared = (
                "macos" if "macintosh" in ual or "mac os x" in ual
                else "linux" if "linux" in ual and "android" not in ual
                else "windows" if "windows" in ual
                else None
            )
            if declared and declared != self.platform:
                problems.append(
                    f"user_agent declares {declared!r} but platform={self.platform!r}"
                )

        if problems:
            raise ValueError(
                f"ProfileSpec {self.name!r} is self-contradictory:\n  - " + "\n  - ".join(problems)
            )

        if (self.screen_width, self.screen_height) not in COMMON_RESOLUTIONS:
            warnings.warn(
                f"{self.screen_width}x{self.screen_height} is an uncommon resolution; "
                "that is extra entropy, make sure it is intentional",
                stacklevel=3,
            )

    # ---------------------------- 序列化 ------------------------------ #

    def to_payload(self) -> dict[str, Any]:
        """转成 ``POST /api/profiles`` 的 JSON body。

        ``None`` 的字段直接丢掉（让 Manager 用默认值），``tags`` 转成
        ``[{"tag": ...}]``，``launch_args`` 转成 list。

        :returns: 可直接 POST 的 dict
        """
        payload: dict[str, Any] = {}
        for f in fields(self):
            value = getattr(self, f.name)
            if value is None:
                continue
            if f.name == "tags":
                if value:
                    payload["tags"] = [{"tag": t} for t in value]
            elif f.name == "launch_args":
                payload["launch_args"] = list(value)
            else:
                payload[f.name] = value
        return payload

    @property
    def viewport_height(self) -> int:
        """实际可视区域高度 = ``screen_height − 133``（Manager 实测）。

        设 ``screen_height`` 时按这个换算预期的 viewport，坐标以它为准。
        """
        return self.screen_height - 133


# --------------------------------------------------------------------------- #
# Provider
# --------------------------------------------------------------------------- #


class CloakBrowserManager(HTTPProvider):
    """CloakBrowser Manager。

    所有实例共用 Manager 的**同一个端口**，靠 URL path 区分 —— 不是每实例一个端口，
    多实例只需要一条隧道。

    :param base_url: Manager 根地址，如 ``http://127.0.0.1:19000``
    :param token: Bearer token。不给就读环境变量 ``SLEIGHT_CLOAK_TOKEN``
    :param name: 池内唯一的 provider 名，也是 uid 前缀
    :param kw: 透传给 :class:`~sleight.providers.base.HTTPProvider` ——
        ``timeout`` / ``ca_bundle`` / ``verify``
    :raises ValueError: 既没传 token 也没有环境变量
    """

    launch_path = "/launch"
    stop_path = "/stop"

    def __init__(
        self,
        base_url: str,
        *,
        token: str | None = None,
        name: str = "cloakbrowser",
        **kw: Any,
    ) -> None:
        self.token = token or os.environ.get(TOKEN_ENV, "")
        if not self.token:
            raise ValueError(
                f"CloakBrowserManager needs a token (argument or ${TOKEN_ENV}); "
                "every endpoint including the WS handshake is authenticated"
            )
        super().__init__(base_url, name=name, **kw)

    # ------------------------------------------------------------------ #

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    def _instance_path(self, instance_id: str, suffix: str) -> str:
        return f"{API}/{instance_id}{suffix}"

    def _ws_url(self, instance_id: str) -> str:
        """从 ``base_url`` 拼，**不用 ``/json/version`` 返回的 ``webSocketDebuggerUrl``**。

        Manager 会把那个字段重写成 ``ws://127.0.0.1:19000/...`` —— host 是**写死的**，
        换隧道端口或从内网直连就错。
        """
        ws_base = self.base_url.replace("https://", "wss://", 1).replace("http://", "ws://", 1)
        return f"{ws_base}{API}/{instance_id}/cdp"

    # ------------------------------------------------------------------ #
    # 发现与状态
    # ------------------------------------------------------------------ #

    def _list(self) -> list[InstanceInfo]:
        r = self._http.get(API)
        if not r.ok or not isinstance(r.body, list):
            raise InstanceError(f"{self.name}: GET {API} returned {r.status}")
        return [self._to_info(p) for p in r.body]

    def _to_info(self, raw: dict[str, Any]) -> InstanceInfo:
        return InstanceInfo(
            id=raw["id"],
            provider=self.name,
            ready=raw.get("status") == "running",
            name=raw.get("name") or "",
            # Manager 返回 [{tag, color}]；color 是展示层信息，驱动层不需要
            tags=frozenset(t["tag"] for t in (raw.get("tags") or []) if t.get("tag")),
        )

    def status(self, instance_id: str) -> InstanceStatus:
        """**唯一**能区分「不存在」和「已停止」的接口。

        ``stop`` 对两者都返回 404 且 detail 完全相同，所以任何生命周期判断都必须
        先走这里。

        :param instance_id: profile id
        :returns: RUNNING / STOPPED / NOT_FOUND
        :raises InstanceError: Manager 返回了别的状态码（502 等）
        :raises AuthError: token 不对
        """
        r = self._http.get(self._instance_path(instance_id, "/status"))
        if r.ok and isinstance(r.body, dict):
            return (
                InstanceStatus.RUNNING
                if r.body.get("status") == "running"
                else InstanceStatus.STOPPED
            )
        if r.status == 404:
            return InstanceStatus.NOT_FOUND
        raise InstanceError(f"{self.name}: status {instance_id} returned {r.status} {r.detail}")

    def system_status(self) -> dict[str, Any]:
        """``GET /api/status``。

        :returns: ``{running_count, binary_version, profiles_total}``
        """
        r = self._http.get("/api/status")
        if not r.ok or not isinstance(r.body, dict):
            raise InstanceError(f"{self.name}: GET /api/status returned {r.status}")
        return r.body

    # ------------------------------------------------------------------ #
    # Profile 组装
    # ------------------------------------------------------------------ #

    def get_profile(self, instance_id: str) -> dict[str, Any]:
        """按 id 取 profile 的完整原始字段。

        :param instance_id: profile id
        :returns: Manager 返回的原始 dict
        :raises NotFound: 没这个 profile
        """
        r = self._http.get(f"{API}/{instance_id}")
        if r.status == 404:
            raise NotFound(f"{self.name}: no such profile {instance_id!r}")
        if not r.ok or not isinstance(r.body, dict):
            raise InstanceError(f"{self.name}: GET profile returned {r.status}")
        return r.body

    def list_profiles(self) -> list[dict[str, Any]]:
        """全部 profile 的**原始**字段。

        :meth:`list_instances` 返回的 :class:`~sleight.core.types.InstanceInfo` 只保留
        驱动层要用的四个字段；运维要看 ``launch_args`` / ``proxy`` / ``notes``，那些只在
        原始 dict 里。

        :returns: Manager 返回的 dict 列表
        """
        r = self._http.get(API)
        if not r.ok or not isinstance(r.body, list):
            raise InstanceError(f"{self.name}: GET {API} returned {r.status}")
        return list(r.body)

    def find_profile(self, name: str) -> dict[str, Any] | None:
        """按**名字**找 profile —— 名字换 id 就靠它。

        :param name: profile 名，精确匹配
        :returns: 原始 dict；没找到返回 ``None``
        """
        return next((p for p in self.list_profiles() if p.get("name") == name), None)

    def cdp_targets(self, instance_id: str) -> list[dict[str, Any]]:
        """``GET /api/profiles/{id}/cdp/json/list`` —— 实例里现在有哪些 CDP target。

        排查"扩展到底加载了没有"就靠它：加载成功的扩展会以
        ``chrome-extension://<32位id>/…`` 出现在这个列表里。MV3 的 service worker
        起来要几秒，太快查会是空的。

        :param instance_id: profile id
        :returns: target 列表。实例没在运行时是空列表（Manager 返回 404）
        :raises InstanceError: Manager 返回了别的状态码
        """
        r = self._http.get(self._instance_path(instance_id, "/cdp/json/list"))
        if r.status == 404:
            return []
        if not r.ok or not isinstance(r.body, list):
            raise InstanceError(f"{self.name}: cdp/json/list returned {r.status} {r.detail}")
        return list(r.body)

    def create_profile(self, spec: ProfileSpec, *, launch: bool = False) -> InstanceInfo:
        """新建一个 profile。**不做去重** —— 同名会再建一个，幂等要用
        :meth:`ensure_profile`。

        :param spec: profile 描述，下发前会先 :meth:`ProfileSpec.validate`
        :param launch: True 则建完立刻拉起并等它 running
        :returns: 新 profile 的 :class:`~sleight.core.types.InstanceInfo`
        :raises ValueError: spec 自相矛盾
        :raises InstanceError: Manager 拒绝了创建请求
        """
        spec.validate()
        r = self._http.post(API, json_body=spec.to_payload())
        if not r.ok or not isinstance(r.body, dict):
            raise InstanceError(f"{self.name}: create profile failed ({r.status}) {r.detail}")
        info = self._to_info(r.body)
        if launch:
            self.ensure_ready(info.id)
            info = replace_ready(info)
        return info

    def ensure_profile(self, spec: ProfileSpec) -> InstanceInfo:
        """按 ``name`` 幂等：存在则更新差异字段，不存在则创建。

        留 ``None`` 的字段不参与比对，也不会被下发 —— 想真正清空某个字段得显式
        调 :meth:`update_profile`。

        :param spec: profile 描述
        :returns: 现有或新建的 :class:`~sleight.core.types.InstanceInfo`
        :raises ValueError: spec 自相矛盾
        """
        spec.validate()
        existing = self.find_profile(spec.name)
        if existing is None:
            return self.create_profile(spec)

        payload = spec.to_payload()
        diff = {k: v for k, v in payload.items() if k != "tags" and existing.get(k) != v}

        # tags 要按归一化形状比：服务端存的是 [{tag, color}]，spec 给的是 [{tag}]，
        # 直接比字典永远不等。以前干脆把 tags 排除在 diff 之外，结果是**改了 tag 重跑
        # 完全不发 PUT**，而 tags 正是 Pool 的路由键（`where=lambda i: "us" in i.tags`），
        # 重新打标的 profile 会继续被旧谓词选中。
        current_tags = sorted(t["tag"] for t in (existing.get("tags") or []) if t.get("tag"))
        if current_tags != sorted(spec.tags):
            diff["tags"] = [{"tag": t} for t in spec.tags]

        if diff:
            log.info("updating profile %r: %s", spec.name, sorted(diff))
            return self.update_profile(existing["id"], **diff)
        return self._to_info(existing)

    def update_profile(self, instance_id: str, **changes: Any) -> InstanceInfo:
        """改若干字段。不重启浏览器，多数字段要下次 launch 才生效。

        :param instance_id: profile id
        :param changes: 字段名 → 新值，直接作为 PUT body
        :returns: 更新后的 :class:`~sleight.core.types.InstanceInfo`
        :raises NotFound: 没这个 profile
        """
        r = self._http.put(f"{API}/{instance_id}", json_body=changes)
        if r.status == 404:
            raise NotFound(f"{self.name}: no such profile {instance_id!r}")
        if not r.ok or not isinstance(r.body, dict):
            raise InstanceError(f"{self.name}: update profile failed ({r.status}) {r.detail}")
        return self._to_info(r.body)

    def delete_profile(self, instance_id: str, *, force: bool = False) -> None:
        """删除 profile。

        默认拒绝删除 running 的实例 —— 删 profile 会连带删掉 ``user_data_dir``
        里的登录态，不可逆。

        :param instance_id: profile id
        :param force: True 则先 stop 再删
        :raises NotFound: 没这个 profile
        :raises InstanceError: 实例在运行且没给 ``force=True``
        """
        st = self.status(instance_id)
        if st is InstanceStatus.NOT_FOUND:
            raise NotFound(f"{self.name}: no such profile {instance_id!r}")
        if st is InstanceStatus.RUNNING:
            if not force:
                raise InstanceError(
                    f"{self.name}: profile {instance_id} is running; "
                    "deleting drops its persistent user_data_dir (logins included). "
                    "Pass force=True if that is what you want."
                )
            self._stop(instance_id, tolerate_stopped=True)
        r = self._http.delete(f"{API}/{instance_id}")
        if not r.ok and r.status != 404:
            raise InstanceError(f"{self.name}: delete profile failed ({r.status}) {r.detail}")

    def stop(self, instance_id: str) -> None:
        """显式停止一个实例。

        **这是运维动作** —— ``release()`` 不会替你调，持久化登录态在浏览器里。

        不传 ``tolerate_stopped``：这是唯一没有前置 ``status()`` 的调用点，容忍 404
        就等于把打错的 instance_id 静默当成幂等成功。

        :param instance_id: profile id
        :raises NotFound: 这个 id 根本不存在（已经停了的不算，那是幂等成功）
        """
        self._stop(instance_id)


def replace_ready(info: InstanceInfo) -> InstanceInfo:
    """返回一个 ``ready=True`` 的副本。"""
    return replace(info, ready=True)
