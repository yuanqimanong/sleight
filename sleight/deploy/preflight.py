"""部署前体检。

每一项都对应一次真实的失败：docker 装了但当前用户不在 docker 组、端口被别的进程占了、
compose 还是 v1、内存不够三个 profile、两个 Manager 挂同一个 ``/data``（手册附录 A.3
的"生产环境禁止"第四条，后果是数据库互相踩）。

解析器全是纯函数，喂样本输出就能测，不需要真机。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

from .runner import Runner
from .spec import DeploySpec, split_image

__all__ = [
    "Check",
    "CheckLevel",
    "parse_df_avail_kb",
    "parse_listening_ports",
    "parse_mem_total_kb",
    "parse_ps_mounts",
    "preflight",
    "worst",
]

#: 三个 profile 的起步线（手册 A.7：每个运行 profile 约 512 MB，但真实页面会显著超）
MIN_MEM_KB = 8 * 1024 * 1024
#: 更稳妥的生产基线
GOOD_MEM_KB = 16 * 1024 * 1024
#: /data 会装浏览器用户目录和备份
MIN_DISK_KB = 20 * 1024 * 1024


class CheckLevel(str, Enum):
    OK = "ok"
    WARN = "warn"
    FAIL = "fail"


@dataclass(frozen=True, slots=True)
class Check:
    """一个检查项。

    :ivar hint: 怎么修。**FAIL 必须给** —— 一个没有下一步的失败等于让人自己猜
    """

    name: str
    level: CheckLevel
    detail: str
    hint: str = ""

    @property
    def ok(self) -> bool:
        return self.level is not CheckLevel.FAIL

    def __str__(self) -> str:
        mark = {CheckLevel.OK: "✓", CheckLevel.WARN: "!", CheckLevel.FAIL: "✗"}[self.level]
        line = f"{mark} {self.name}: {self.detail}"
        return f"{line}\n    → {self.hint}" if self.hint else line


def worst(checks: list[Check]) -> CheckLevel:
    """一组检查里最坏的那一级。"""
    for level in (CheckLevel.FAIL, CheckLevel.WARN):
        if any(c.level is level for c in checks):
            return level
    return CheckLevel.OK


# --------------------------------------------------------------------------- #
# 解析器
# --------------------------------------------------------------------------- #


def parse_mem_total_kb(meminfo: str) -> int | None:
    """从 ``/proc/meminfo`` 取 ``MemTotal``，单位 KB。"""
    m = re.search(r"^MemTotal:\s+(\d+)\s*kB", meminfo, re.M)
    return int(m.group(1)) if m else None


def parse_df_avail_kb(output: str) -> int | None:
    """从 ``df -Pk`` 取可用空间，单位 KB。

    ``-P`` 保证一行一条记录（长设备名默认会折行），所以取最后一行的第 4 列。
    """
    rows = [ln.split() for ln in output.strip().splitlines()[1:] if ln.strip()]
    for row in reversed(rows):
        if len(row) >= 4 and row[3].isdigit():
            return int(row[3])
    return None


def parse_listening_ports(output: str) -> set[int]:
    """从 ``ss -ltn`` 或 ``netstat -ltn`` 的输出里取本地监听端口。

    两者的第 4 列都是本地地址，所以同一段代码能吃下去。要处理 ``*:9000``、
    ``[::]:9000``、``127.0.0.1:9000`` 三种写法。
    """
    ports: set[int] = set()
    for line in output.splitlines():
        cols = line.split()
        if len(cols) < 4:
            continue
        if cols[0].lower() in ("state", "proto", "active", "netid"):
            continue
        local = cols[3]
        _, _, tail = local.rpartition(":")
        if tail.isdigit():
            ports.add(int(tail))
    return ports


def parse_ps_mounts(output: str) -> dict[str, set[str]]:
    """解析 ``docker ps -a --format '{{.Names}}\\t{{.Mounts}}'``。

    :returns: 容器名 → 它挂了的宿主机路径集合
    """
    out: dict[str, set[str]] = {}
    for line in output.splitlines():
        name, tab, mounts = line.partition("\t")
        if not tab or not name.strip():
            continue
        out[name.strip()] = {m.strip() for m in mounts.split(",") if m.strip()}
    return out


# --------------------------------------------------------------------------- #
# 体检
# --------------------------------------------------------------------------- #


def preflight(spec: DeploySpec, runner: Runner, *, sudo: bool = False) -> list[Check]:
    """把目标机体检一遍。**只读，不改任何东西。**

    :param spec: 部署描述
    :param runner: 在哪台机器上检
    :param sudo: 部署时会不会用 ``sudo`` —— 影响目录权限那一项是 FAIL 还是 OK
    :returns: 检查项列表，顺序即建议的阅读顺序
    """
    checks: list[Check] = []
    add = checks.append

    # —— 能不能连上 ——
    probe = runner.run(["uname", "-sm"])
    if not probe.ok:
        add(Check(
            "host", CheckLevel.FAIL, f"cannot run commands on {runner.label}",
            hint=(probe.err.strip() or "check the ssh target, key and network").splitlines()[-1],
        ))
        return checks                                  # 后面全都依赖它，没必要继续
    add(Check("host", CheckLevel.OK, f"{runner.label} — {probe.text}"))

    # —— docker ——
    version = runner.run(["docker", "version", "--format", "{{.Server.Version}}"])
    if not version.ok:
        err = version.err.strip() or version.out.strip()
        hint = "install docker: https://docs.docker.com/engine/install/"
        if "permission denied" in err.lower():
            hint = (
                "the docker socket is not accessible: sudo usermod -aG docker $USER, "
                "then reconnect (group membership only applies to new sessions)"
            )
        elif "cannot connect" in err.lower() or "daemon" in err.lower():
            hint = "the daemon is not running: sudo systemctl enable --now docker"
        add(Check("docker", CheckLevel.FAIL, err.splitlines()[-1] if err else "docker unavailable",
                  hint=hint))
        return checks                                  # 没 docker 后面的检查都没意义
    add(Check("docker", CheckLevel.OK, f"engine {version.text}"))

    compose = runner.run(["docker", "compose", "version", "--short"])
    if not compose.ok:
        legacy = runner.run(["docker-compose", "--version"])
        hint = (
            "only compose v2 (the 'docker compose' plugin) is supported; "
            "docker-compose v1 is end-of-life"
            if legacy.ok
            else "install the plugin: apt install docker-compose-plugin (or Docker Desktop)"
        )
        add(Check("compose", CheckLevel.FAIL, "'docker compose' is not available", hint=hint))
    else:
        add(Check("compose", CheckLevel.OK, f"compose v{compose.text.lstrip('v')}"))

    # —— 目录与权限 ——
    add(_check_dir(spec, runner, sudo=sudo))

    # —— 端口 ——
    add(_check_port(spec, runner))

    # —— /data 独占 ——
    add(_check_data_conflict(spec, runner))

    # —— 内存 / 磁盘 ——
    add(_check_memory(runner))
    add(_check_disk(spec, runner))

    # —— 镜像 ——
    image = _check_image(spec, runner)
    add(image)
    if "will be pulled" in image.detail:
        add(_check_registry(runner))

    return checks


def _existing_ancestor(path: str, runner: Runner) -> str:
    """往上找到第一个真实存在的祖先目录 —— 权限和空间要在那上面量。"""
    current = path.rstrip("/")
    while current and current != "/":
        if runner.run(["test", "-d", current]).ok:
            return current
        current = current.rsplit("/", 1)[0]
    return "/"


def _check_dir(spec: DeploySpec, runner: Runner, *, sudo: bool) -> Check:
    if runner.run(["test", "-d", spec.dir]).ok:
        if runner.run(["test", "-w", spec.dir]).ok:
            return Check("dir", CheckLevel.OK, f"{spec.dir} exists and is writable")
        detail = f"{spec.dir} exists but is not writable by this user"
    else:
        anchor = _existing_ancestor(spec.dir, runner)
        if runner.run(["test", "-w", anchor]).ok:
            return Check("dir", CheckLevel.OK, f"{spec.dir} will be created under {anchor}")
        detail = f"{spec.dir} does not exist and {anchor} is not writable"

    if sudo:
        return Check("dir", CheckLevel.OK, f"{detail} — will use sudo -n")
    return Check(
        "dir", CheckLevel.FAIL, detail,
        hint=(
            "pass --sudo (needs passwordless sudo on the target), or pick a directory you own, "
            "e.g. --dir ~/cloakbrowser-manager"
        ),
    )


def _check_port(spec: DeploySpec, runner: Runner) -> Check:
    listing = runner.run(["ss", "-ltn"])
    if not listing.ok:
        listing = runner.run(["netstat", "-ltn"])
    if not listing.ok:
        return Check("port", CheckLevel.WARN, f"cannot inspect listening ports for {spec.port}",
                     hint="neither ss nor netstat is available; docker will report the conflict")
    if spec.port not in parse_listening_ports(listing.out):
        return Check("port", CheckLevel.OK, f"{spec.bind_ip}:{spec.port} is free")

    mine = runner.run(["docker", "ps", "--filter", f"name=^{spec.container_name}$",
                       "--format", "{{.Names}}"])
    if mine.ok and spec.container_name in mine.text.splitlines():
        return Check("port", CheckLevel.OK,
                     f"{spec.port} is held by {spec.container_name} — this is a redeploy")
    return Check(
        "port", CheckLevel.FAIL, f"port {spec.port} is already in use by something else",
        hint=f"pick another port (--port), or free it: ss -ltnp | grep :{spec.port}",
    )


def _check_data_conflict(spec: DeploySpec, runner: Runner) -> Check:
    """两个 Manager 挂同一个 ``/data`` 会互相踩数据库。这是最贵的一种错。"""
    listing = runner.run(["docker", "ps", "-a", "--format", "{{.Names}}\t{{.Mounts}}"])
    if not listing.ok:
        return Check("data", CheckLevel.WARN, "cannot list containers to check for /data sharing")
    for name, mounts in parse_ps_mounts(listing.out).items():
        if name == spec.container_name:
            continue
        if spec.data_dir in mounts or spec.dir in mounts:
            return Check(
                "data", CheckLevel.FAIL,
                f"container {name!r} already mounts {spec.data_dir}",
                hint=(
                    "two managers on one data directory corrupt each other's profile database; "
                    "give this deployment its own --dir"
                ),
            )
    return Check("data", CheckLevel.OK, f"{spec.data_dir} is not mounted by another container")


def _check_memory(runner: Runner) -> Check:
    meminfo = runner.run(["cat", "/proc/meminfo"])
    total = parse_mem_total_kb(meminfo.out) if meminfo.ok else None
    if total is None:
        return Check("memory", CheckLevel.WARN, "cannot read /proc/meminfo")
    gb = total / 1024 / 1024
    if total < MIN_MEM_KB:
        return Check(
            "memory", CheckLevel.WARN, f"{gb:.1f} GB RAM",
            hint=f"3 running profiles want {MIN_MEM_KB // 1024 // 1024} GB minimum, "
                 f"{GOOD_MEM_KB // 1024 // 1024} GB is the recommended production baseline",
        )
    if total < GOOD_MEM_KB:
        return Check("memory", CheckLevel.OK,
                     f"{gb:.1f} GB RAM — fine for ~3 profiles, 16 GB is the recommended baseline")
    return Check("memory", CheckLevel.OK, f"{gb:.1f} GB RAM")


def _check_disk(spec: DeploySpec, runner: Runner) -> Check:
    anchor = _existing_ancestor(spec.dir, runner)
    df = runner.run(["df", "-Pk", anchor])
    avail = parse_df_avail_kb(df.out) if df.ok else None
    if avail is None:
        return Check("disk", CheckLevel.WARN, f"cannot read free space on {anchor}")
    gb = avail / 1024 / 1024
    if avail < MIN_DISK_KB:
        return Check(
            "disk", CheckLevel.WARN, f"{gb:.1f} GB free on {anchor}",
            hint="the manager image plus browser user data and backups will not be comfortable "
                 f"under {MIN_DISK_KB // 1024 // 1024} GB",
        )
    return Check("disk", CheckLevel.OK, f"{gb:.1f} GB free on {anchor}")


def _check_image(spec: DeploySpec, runner: Runner) -> Check:
    _, tag, digest = split_image(spec.image)
    local = runner.run(["docker", "image", "inspect", spec.image, "--format", "{{.Id}}"])
    where = "already pulled" if local.ok else "will be pulled"
    pin = "digest-pinned" if digest else f"tag {tag!r}"
    return Check("image", CheckLevel.OK, f"{spec.image} — {pin}, {where}")


def _check_registry(runner: Runner) -> Check:
    """要拉镜像时，确认 **daemon** 到得了 registry。

    ``docker pull`` 是 systemd 起的 root 守护进程干的活，它**读不到你 shell 里的
    ``HTTP_PROXY``**。错配的症状是 pull 卡满超时然后吐一句
    ``context deadline exceeded``，很容易被当成网络抖动，真实原因要翻半天。

    这里只比对两边：shell 有代理而 daemon 没有 —— 那就基本准备好失败了。
    """
    daemon = runner.run(["docker", "info", "--format", "{{.HTTPProxy}}|{{.HTTPSProxy}}"])
    shell = runner.run(
        ["sh", "-c", 'printf %s "${HTTPS_PROXY:-${https_proxy:-${HTTP_PROXY:-$http_proxy}}}"']
    )
    # docker info 给的是 "HTTP|HTTPS" 两个值，通常一样 —— 去重后只报一个
    seen = [v for v in daemon.text.split("|") if v.strip()] if daemon.ok else []
    daemon_proxy = ", ".join(dict.fromkeys(seen))
    shell_proxy = shell.text.strip()

    if daemon_proxy:
        return Check("registry", CheckLevel.OK, f"the daemon uses a proxy ({daemon_proxy})")
    if not shell_proxy:
        return Check("registry", CheckLevel.OK, "no proxy in play — the daemon pulls directly")
    return Check(
        "registry", CheckLevel.WARN,
        f"your shell goes through {shell_proxy} but the docker daemon has no proxy configured",
        hint=(
            "the daemon is a systemd service and does not inherit your environment, so the pull "
            "will very likely time out with 'context deadline exceeded'. Give it the proxy:\n"
            "      sudo mkdir -p /etc/systemd/system/docker.service.d\n"
            "      printf '[Service]\\nEnvironment=\"HTTP_PROXY=%s\"\\nEnvironment=\"HTTPS_PROXY=%s\"\\n"
            'Environment="NO_PROXY=localhost,127.0.0.1"\\n\' "$HTTP_PROXY" "$HTTPS_PROXY" '
            "| sudo tee /etc/systemd/system/docker.service.d/proxy.conf\n"
            "      sudo systemctl daemon-reload && sudo systemctl restart docker"
        ),
    )
