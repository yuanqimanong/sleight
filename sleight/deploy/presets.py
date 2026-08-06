"""部署与实例的**模板**，以及每个选项的解释和推荐值。

界面和命令行都从这里取，所以"某个字段是干什么的、推荐填什么"只有一处定义。写死在
前端 HTML 里的话，加一个字段就要改两个地方，然后其中一个会忘。

三类东西：

* :data:`DEPLOY_TEMPLATES` —— 建一个 Manager 环境时的几套起手式，按规模分档。
  数字来自手册附录 A.7 / A.11 的实测与压测建议，不是拍的。
* :data:`PROFILE_PRESETS` —— 浏览器实例的身份模板，直接映射到
  :class:`~sleight.providers.ProfileSpec` 的四个预设工厂。**预设的价值是保证指纹
  自洽**：平台、时区、语言、GPU 串必须是同一台机器上可能出现的组合。
* :data:`FIELD_HELP` —— 每个可填字段的一句话解释 + 推荐值。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .spec import DEFAULT_IMAGE, DeploySpec

__all__ = [
    "DEPLOY_TEMPLATES",
    "FIELD_HELP",
    "PROFILE_PRESETS",
    "DeployTemplate",
    "FieldHelp",
    "ProfilePreset",
    "profile_spec_from",
]


@dataclass(frozen=True, slots=True)
class FieldHelp:
    """一个字段的解释。

    :param why: **为什么有这个字段** —— 不是重复字段名，而是说清填错会怎样
    :param recommend: 推荐值的说法（"默认就行"也是一种）
    """

    label: str
    why: str
    recommend: str = ""

    def to_dict(self) -> dict[str, str]:
        return {"label": self.label, "why": self.why, "recommend": self.recommend}


#: 字段名 → 解释。界面上每个输入框旁边那行小字就是它
FIELD_HELP: dict[str, FieldHelp] = {
    # —— 主机 ——
    "host_name": FieldHelp(
        "主机名字", "你自己起的代号，之后 --host 用它。跟机器的 hostname 无关。",
        "用能一眼认出的，比如 hk-01、prod-browser",
    ),
    "ssh": FieldHelp(
        "SSH 目标", "user@host，或 ~/.ssh/config 里的别名。留空表示就在这台机器上部署。",
        "填 ~/.ssh/config 里的别名最省事 —— 端口和私钥都不用再填一遍",
    ),
    "ssh_port": FieldHelp("SSH 端口", "非 22 才需要填。", "留空 = 22"),
    "identity": FieldHelp(
        "私钥", "指定用哪个私钥。ssh-agent 里有或 ssh_config 里配了就不用填。",
        "留空，让 ssh 按你平时的方式认证",
    ),
    "sudo": FieldHelp(
        "用 sudo 建目录",
        "只影响建目录和改权限，docker 命令永远不加 sudo。目标机必须配了免密 sudo，"
        "否则会当场失败（不会挂住等密码）。",
        "把部署目录放在自己家目录下就不用开它",
    ),
    "accept_new": FieldHelp(
        "首次连接信任指纹",
        "相当于 StrictHostKeyChecking=accept-new。不勾就沿用你自己的 ssh 配置。",
        "只在第一次连新机器时勾",
    ),
    "notes": FieldHelp("备注", "给人看的，比如这台机在哪个机房、归谁。", ""),
    # —— 部署 ——
    "deployment_name": FieldHelp(
        "这个 Manager 的名字",
        "一台机器可以跑多个 Manager。这个名字会被拼进 compose 项目名和容器名，"
        "所以只能用小写字母、数字、'-' 和 '_'。",
        "第一个叫 default，后面的按用途起，比如 hk / staging",
    ),
    "dir": FieldHelp(
        "部署目录",
        "compose 文件、.env 和 data/ 都在这里。data/ 里是 profile 数据库、指纹种子、"
        "Cookie 和全部登录态 —— 换目录等于换一套全新的浏览器。",
        f"自己有权限的路径最省事，比如 ~/cloakbrowser-manager；系统级用 {DeploySpec().dir}",
    ),
    "image": FieldHelp(
        "镜像",
        "生产上必须钉具体版本。latest 会被拒绝 —— 它不可追溯，出事了不知道回滚到哪。",
        f"{DEFAULT_IMAGE}；要完全可复现就再钉摘要（repo:tag@sha256:…）",
    ),
    "port": FieldHelp(
        "宿主机端口",
        "映射到容器里的 8080。所有 profile 共用这一个端口，靠 URL path 区分，"
        "不是每个实例一个端口。",
        "9000；同一台机上第二个 Manager 换成 9001",
    ),
    "bind_ip": FieldHelp(
        "监听地址",
        "127.0.0.1 表示只有目标机本身能连，外面要访问得走 SSH 隧道。填别的地址就是"
        "把它暴露到网络上，而 AUTH_TOKEN 在 HTTP 上是明文的。",
        "127.0.0.1，然后用 sleight tunnel 访问",
    ),
    "expose": FieldHelp(
        "确认要监听非回环地址",
        "只有勾了才允许填非 127.0.0.1 的地址。前面必须有防火墙、私网或 TLS 代理。",
        "不勾",
    ),
    "shm_size": FieldHelp(
        "/dev/shm 大小",
        "Chromium 用共享内存放渲染缓冲，不够会莫名崩标签页。这是**整个容器共享**的，"
        "不是每个 profile 一份。",
        "3 个 profile 用 5gb；再多按压测调",
    ),
    "container_name": FieldHelp(
        "容器名", "docker 里的名字，同一台机上唯一。",
        "留空自动生成（第二个 Manager 会自动加后缀，不会覆盖第一个）",
    ),
    # —— 实例 ——
    "profile_name": FieldHelp(
        "实例名字", "Manager 里唯一。代码里可以按名字租：where=lambda i: i.name == '…'。",
        "带上用途和地区，比如 news-us-01",
    ),
    "preset": FieldHelp(
        "身份模板",
        "决定平台、时区、语言和 GPU 串。**这四样必须自洽** —— 声称是 Windows 却报"
        "一串 Apple Metal 的 renderer，是一眼就会被标记的矛盾。",
        "按目标站点期望的访客地区选",
    ),
    "proxy": FieldHelp(
        "代理", "socks5://user:pass@host:port 或 http://…。出口 IP 决定对方看到你在哪。",
        "跟身份模板的地区对上，不然时区和 IP 打架",
    ),
    "geoip": FieldHelp(
        "按代理出口 IP 推导时区和语言",
        "勾了就由出口 IP 说了算，不能再手填时区/语言（会冲突）。需要先配代理。",
        "换着用多个地区的代理时勾它",
    ),
    "tags": FieldHelp(
        "标签", "逗号分隔。Pool 靠它路由：where=lambda i: 'us' in i.tags。",
        "把地区和用途打上，比如 us,news",
    ),
    "screen": FieldHelp(
        "分辨率",
        "罕见分辨率本身就是熵。可视区域高度 = 屏高 − 133。",
        "1920x1080",
    ),
    "headless": FieldHelp(
        "无头",
        "无头浏览器有额外的可检测特征，反检测场景通常不要开。Manager 自带 Xvnc，"
        "有头也不需要真显示器。",
        "不勾",
    ),
    "fingerprint_seed": FieldHelp(
        "指纹种子", "固定它，同一个 profile 每次启动的指纹才可复现。留空则由 Manager 随机。",
        "留空；要复现问题时再固定",
    ),
}


# --------------------------------------------------------------------------- #
# 部署模板
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class DeployTemplate:
    """建 Manager 环境的一套起手式。"""

    key: str
    label: str
    summary: str
    detail: str
    overrides: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key, "label": self.label, "summary": self.summary,
            "detail": self.detail, "spec": DeploySpec(**self.overrides).to_dict(),
        }


#: 按规模分档。shm 和内存的数字来自手册 A.7 / A.11
DEPLOY_TEMPLATES: tuple[DeployTemplate, ...] = (
    DeployTemplate(
        "trial", "试一下",
        "1～2 个实例，够跑通流程",
        "shm 2gb，只听本机。内存 4 GB 的机器也能起来。先确认整条链路通了再往上加。",
        {"shm_size": "2gb"},
    ),
    DeployTemplate(
        "standard", "标准（约 3 个实例）",
        "手册里的推荐形态，绝大多数情况选这个",
        "shm 5gb，只听本机。3 个实例建议 8 GB 内存起步，16 GB 更稳妥。"
        "不要为每个实例单独跑一个 Manager —— 拆分的依据是资源隔离和故障域，不是实例数量。",
        {"shm_size": "5gb"},
    ),
    DeployTemplate(
        "large", "较大（约 5 个实例）",
        "单机再往上就该拆机器了",
        "shm 8gb。建议 16 GB 内存，并按真实页面压测调整。到 10 个实例左右应该改成"
        "两个 Manager 或两台机器，各自独立端口和 data/，缩小故障域。",
        {"shm_size": "8gb"},
    ),
    DeployTemplate(
        "private-net", "私网直连",
        "任务节点和它在同一个内网，不想走隧道",
        "监听 0.0.0.0 而不是 127.0.0.1。**AUTH_TOKEN 在 HTTP 上是明文的** —— "
        "必须有防火墙只放行任务节点，或者前面加 TLS 代理。",
        {"shm_size": "5gb", "bind_ip": "0.0.0.0", "expose": True},
    ),
)


# --------------------------------------------------------------------------- #
# 实例模板
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ProfilePreset:
    """浏览器实例的身份模板，对应 ``ProfileSpec`` 的一个预设工厂。"""

    key: str
    label: str
    summary: str
    factory: str

    def to_dict(self) -> dict[str, Any]:
        from ..providers.cloakbrowser import ProfileSpec

        sample = getattr(ProfileSpec, self.factory)("sample")
        return {
            "key": self.key, "label": self.label, "summary": self.summary,
            "platform": sample.platform, "timezone": sample.timezone,
            "locale": sample.locale, "gpu": sample.gpu_renderer,
        }


PROFILE_PRESETS: tuple[ProfilePreset, ...] = (
    ProfilePreset("windows_us", "Windows / 美国", "最常见的访客画像", "windows_us"),
    ProfilePreset("windows_hk", "Windows / 香港", "中文站点或亚太出口", "windows_hk"),
    ProfilePreset("macos_us", "macOS / 美国", "需要 Safari 之外的 Mac 画像时", "macos_us"),
    ProfilePreset("linux_us", "Linux / 美国", "Mesa 软渲染，画像最不常见", "linux_us"),
)


def profile_spec_from(preset: str, name: str, **overrides: Any) -> Any:
    """按模板造一个 ``ProfileSpec``。

    :param preset: :data:`PROFILE_PRESETS` 里的 key
    :param name: 实例名
    :param overrides: 覆盖任意字段（proxy / tags / geoip / headless …）
    :returns: :class:`~sleight.providers.ProfileSpec`
    :raises ValueError: 没这个模板，或覆盖出了自相矛盾的组合
    """
    from ..providers.cloakbrowser import ProfileSpec

    chosen = next((p for p in PROFILE_PRESETS if p.key == preset), None)
    if chosen is None:
        known = ", ".join(p.key for p in PROFILE_PRESETS)
        raise ValueError(f"unknown profile preset {preset!r}; known: {known}")
    clean = {k: v for k, v in overrides.items() if v not in (None, "", (), [])}
    return getattr(ProfileSpec, chosen.factory)(name, **clean)
