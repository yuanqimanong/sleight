"""一份部署的完整描述。

``DeploySpec`` 是纯数据 —— 不碰网络、不碰磁盘。渲染（:mod:`sleight.deploy.render`）
和执行（:mod:`sleight.deploy.engine`）都从它出发，所以 ``--dry-run`` 能在不接触目标机
的前提下把最终结果原样打出来。
"""

from __future__ import annotations

import ipaddress
import re
import secrets
from dataclasses import asdict, dataclass, fields, replace
from typing import Any

__all__ = ["DEFAULT_IMAGE", "DeploySpec", "generate_token", "split_image"]

#: 手册附录 A.9 的版本基线。生产上应显式钉更具体的标签或摘要。
DEFAULT_IMAGE = "cloakhq/cloakbrowser-manager:v0.0.10"

#: Manager 在容器里监听的端口。它不可配 —— 是镜像写死的。
CONTAINER_PORT = 8080

#: 容器内的数据根。挂载点固定，改了 Manager 就找不到自己的库。
CONTAINER_DATA = "/data"

_PROJECT_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_CONTAINER_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]+$")
_SIZE_RE = re.compile(r"^\d+(\.\d+)?(b|k|kb|m|mb|g|gb)$", re.I)
_DURATION_RE = re.compile(r"^\d+(\.\d+)?(ns|us|ms|s|m|h)$")
_RESTART = frozenset({"no", "always", "unless-stopped", "on-failure"})


def generate_token() -> str:
    """生成一个 64 位十六进制的 ``AUTH_TOKEN``，等价于手册里的 ``openssl rand -hex 32``。"""
    return secrets.token_hex(32)


def split_image(image: str) -> tuple[str, str, str]:
    """把镜像引用拆成 ``(仓库, 标签, 摘要)``，缺的部分是空串。

    要处理 ``registry:5000/ns/name:tag@sha256:…`` 这种 —— 冒号既可能是端口也可能是
    标签分隔符，只能从右往左、且只在最后一个 ``/`` 之后找。

    :param image: 完整镜像引用
    :returns: ``(repo, tag, digest)``
    """
    rest, _, digest = image.partition("@")
    slash = rest.rfind("/")
    colon = rest.rfind(":")
    if colon > slash:
        return rest[:colon], rest[colon + 1 :], digest
    return rest, "", digest


@dataclass(frozen=True, slots=True)
class DeploySpec:
    """一个 Manager 部署。

    默认值直接对齐手册附录 A —— 照抄那一节手工部署出来的东西，和这里 ``apply()``
    出来的东西是同一个形状。

    :param name: compose 项目名，同一台机上唯一
    :param dir: 目标机上的部署目录，必须是绝对路径
    :param image: 镜像引用，生产上钉标签或摘要
    :param container_name: 容器名，同一台机上唯一
    :param bind_ip: 宿主机监听地址。非回环地址必须同时 ``expose=True``
    :param port: 宿主机端口，映射到容器的 8080
    :param shm_size: ``/dev/shm`` 大小，**整个容器共享**，不是每 profile
    :param nofile: 文件描述符上限
    :param expose: 明确承认要监听非回环地址（token 在 HTTP 上是明文）
    :param allow_latest: 允许用 ``latest`` 这种不可追溯的标签
    """

    name: str = "cloakbrowser"
    dir: str = "/srv/cloakbrowser-manager"
    image: str = DEFAULT_IMAGE
    container_name: str = "cloakbrowser-manager"
    bind_ip: str = "127.0.0.1"
    port: int = 9000
    # 资源
    shm_size: str = "5gb"
    nofile: int = 65535
    stop_grace_period: str = "45s"
    restart: str = "unless-stopped"
    log_max_size: str = "20m"
    log_max_file: int = 5
    # 显式开关
    expose: bool = False
    allow_latest: bool = False

    # ------------------------------------------------------------------ #
    # 派生路径。目标机一律是 Linux，所以这里写死 POSIX 分隔符，不用 pathlib ——
    # 从 Windows 控制机部署时 pathlib 会拼出反斜杠。
    # ------------------------------------------------------------------ #

    @property
    def compose_path(self) -> str:
        return f"{self.dir}/docker-compose.yaml"

    @property
    def env_path(self) -> str:
        return f"{self.dir}/.env"

    @property
    def state_path(self) -> str:
        """记录"上次部署成的是什么"，升级回滚要靠它找回旧镜像。"""
        return f"{self.dir}/.sleight-deploy.json"

    @property
    def data_dir(self) -> str:
        return f"{self.dir}/data"

    @property
    def backups_dir(self) -> str:
        return f"{self.dir}/backups"

    @property
    def extensions_dir(self) -> str:
        """插件目录的**宿主机**路径。"""
        return f"{self.data_dir}/extensions"

    @property
    def container_extensions_dir(self) -> str:
        """插件目录的**容器内**路径。

        ``launch_args`` 里必须填这个 —— 填宿主机路径 Chromium 会静默不加载，
        两者只差一个前缀，是最常见的错。
        """
        return f"{CONTAINER_DATA}/extensions"

    @property
    def local_url(self) -> str:
        """在**目标机上**访问 Manager 的地址。控制机不一定能直接连这个。"""
        return f"http://127.0.0.1:{self.port}"

    @property
    def bound_url(self) -> str:
        """Manager 实际监听的地址。``bind_ip`` 是回环时和 :attr:`local_url` 相同。"""
        host = self.bind_ip
        if ":" in host:                                   # IPv6 字面量要加方括号
            host = f"[{host}]"
        return f"http://{host}:{self.port}"

    @property
    def reachable_from_control(self) -> bool:
        """控制机能不能直接连到它 —— 回环地址意味着只能靠隧道或在目标机上执行。"""
        try:
            return not ipaddress.ip_address(self.bind_ip).is_loopback
        except ValueError:
            return True

    # ------------------------------------------------------------------ #

    def replace(self, **kw: Any) -> DeploySpec:
        """派生一个改了若干字段的新 spec，原对象不变。**不做校验**。"""
        return replace(self, **kw)

    def to_dict(self) -> dict[str, Any]:
        """转成可 JSON / TOML 序列化的 dict。"""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DeploySpec:
        """从 dict 还原。**未知字段直接忽略** —— 老版本写的状态文件要能被新版本读。"""
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})

    # ------------------------------------------------------------------ #

    def validate(self) -> None:
        """拦下会让部署变成事故的组合。

        这些不是风格问题，每一条都对应手册附录 A.3 的"生产环境禁止"或一次真实的
        排错经历。

        :raises ValueError: 存在硬冲突，消息里逐条列出
        """
        problems: list[str] = []

        if not _PROJECT_RE.match(self.name):
            problems.append(
                f"name={self.name!r} is not a valid compose project name "
                "(lowercase letters, digits, '_' and '-', must not start with '-')"
            )
        if not _CONTAINER_RE.match(self.container_name):
            problems.append(f"container_name={self.container_name!r} is not a valid container name")

        if not self.dir.startswith("/"):
            problems.append(f"dir={self.dir!r} must be an absolute path on the target host")
        if any(c in self.dir for c in "\n\r\t\0"):
            problems.append("dir contains a control character")
        if self.dir.rstrip("/") in ("", "/"):
            problems.append("dir must not be the filesystem root")

        repo, tag, digest = split_image(self.image)
        if not repo:
            problems.append(f"image={self.image!r} has no repository part")
        if not tag and not digest:
            problems.append(
                f"image={self.image!r} has no tag or digest; an implicit ':latest' is exactly "
                "the untraceable production version the manual warns about"
            )
        elif tag == "latest" and not digest and not self.allow_latest:
            problems.append(
                "image tag is 'latest', which is not reproducible — pin a version tag or a "
                "digest, or pass allow_latest=True if you really mean it"
            )

        try:
            loopback = ipaddress.ip_address(self.bind_ip).is_loopback
        except ValueError:
            problems.append(f"bind_ip={self.bind_ip!r} is not a valid IP address")
        else:
            if not loopback and not self.expose:
                problems.append(
                    f"bind_ip={self.bind_ip!r} is not a loopback address. AUTH_TOKEN travels in "
                    "cleartext over HTTP — pass expose=True only behind a private network, "
                    "firewall, VPN or TLS proxy"
                )

        if not 1 <= self.port <= 65535:
            problems.append(f"port={self.port} is out of range")

        if not _SIZE_RE.match(self.shm_size):
            problems.append(f"shm_size={self.shm_size!r} is not a docker size (e.g. '5gb', '512m')")
        if not _SIZE_RE.match(self.log_max_size):
            problems.append(f"log_max_size={self.log_max_size!r} is not a docker size")
        if not _DURATION_RE.match(self.stop_grace_period):
            problems.append(
                f"stop_grace_period={self.stop_grace_period!r} is not a docker duration "
                "(e.g. '45s', '2m')"
            )
        if self.restart not in _RESTART:
            problems.append(
                f"restart={self.restart!r} must be one of {', '.join(sorted(_RESTART))}"
            )
        if self.nofile < 1024:
            problems.append(f"nofile={self.nofile} is too low for a browser fleet")
        if self.log_max_file < 1:
            problems.append(f"log_max_file={self.log_max_file} must be at least 1")

        if problems:
            raise ValueError(
                f"DeploySpec {self.name!r} is not deployable:\n  - " + "\n  - ".join(problems)
            )

    def warnings(self) -> list[str]:
        """不阻塞部署、但值得说一句的东西。"""
        out: list[str] = []
        _, _, digest = split_image(self.image)
        if not digest:
            out.append(
                "image is pinned by tag, not by digest; a tag can be repointed upstream. "
                "Use repo:tag@sha256:… when you need full reproducibility."
            )
        if self.expose and self.reachable_from_control:
            out.append(
                f"the manager will listen on {self.bind_ip}:{self.port} and its AUTH_TOKEN is "
                "sent in cleartext — make sure a firewall, private network or TLS proxy is in "
                "front of it"
            )
        if self.port < 1024:
            out.append(f"port {self.port} is privileged; the docker daemon can bind it, you cannot")
        return out
