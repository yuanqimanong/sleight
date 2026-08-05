"""插件与 profile 的日常运维。

这一层解决的不是"部署"，而是部署完之后**反复要做、每次都容易漏一步**的那些事。
背景（都是踩出来的，不是设计出来的）：

* 插件不由爬虫加载，只能靠 Chromium 启动参数。``--load-extension`` 必须和
  ``--disable-extensions-except`` 成对出现，否则前者指定的插件也会被一起禁掉。
* 两个参数里填的是**容器内**路径。填宿主机路径 Chromium 静默不加载，两者只差一个前缀。
* ``PUT /api/profiles/{id}`` 是**整字段替换**。手工加一个插件时忘了把原有路径写进去，
  等于把旧插件摘掉了。
* 参数只在浏览器**进程启动时**读。改完必须停一次实例。
* **必须遍历每一个 profile。** ``lease()`` 默认随机租一个空闲实例，漏掉一个就意味着
  有一定概率租到没插件的那台 —— 表现为"有时候能过付费墙，有时候不能"。
* 容器里的浏览器进程不是 root（实测 uid 1001）。root 独占的目录它读不到，所以推上去
  之后必须 ``chmod -R a+rX``。

核心的合并逻辑（:func:`merge_launch_args`）是纯函数，可以直接单测。
"""

from __future__ import annotations

import json
import logging
import re
import shlex
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ..core.errors import NotFound
from ..core.types import InstanceStatus
from ..providers.cloakbrowser import CloakBrowserManager
from .engine import Deployer
from .errors import DeployError

log = logging.getLogger("sleight.deploy.ops")

__all__ = [
    "EXTENSION_FLAGS",
    "Extension",
    "ExtensionOps",
    "ProfileOps",
    "extension_paths",
    "merge_launch_args",
]

#: 两个必须成对出现的 Chromium 参数
EXTENSION_FLAGS = ("--disable-extensions-except", "--load-extension")

_EXT_ID_RE = re.compile(r"chrome-extension://([a-p]{32})")


# --------------------------------------------------------------------------- #
# 纯函数
# --------------------------------------------------------------------------- #


def extension_paths(launch_args: Sequence[str]) -> list[str]:
    """从 ``launch_args`` 里读出当前配置的扩展路径。

    以 ``--load-extension`` 为准 —— 它才是真正加载的那个，
    ``--disable-extensions-except`` 只是白名单。

    :param launch_args: profile 的 ``launch_args``
    :returns: 路径列表，保持原有顺序
    """
    for arg in launch_args:
        if arg.startswith("--load-extension="):
            value = arg.split("=", 1)[1]
            return [p for p in (p.strip() for p in value.split(",")) if p]
    return []


def merge_launch_args(current: Sequence[str], paths: Sequence[str]) -> list[str]:
    """算出新的 ``launch_args``：换掉两个扩展参数，**其它参数一律原样保留**。

    保留是关键 —— 在 Manager UI 里手调过的 ``--lang``、``--proxy-bypass-list`` 之类
    不能因为改插件就被抹掉。

    :param current: 现有的 ``launch_args``
    :param paths: 要加载的**容器内**路径。空列表表示把插件全摘掉
    :returns: 新的 ``launch_args``。扩展参数排在末尾，顺序稳定
    """
    kept = [a for a in current if not a.startswith(tuple(f"{f}=" for f in EXTENSION_FLAGS))]
    kept = [a for a in kept if a not in EXTENSION_FLAGS]
    if not paths:
        return kept
    joined = ",".join(paths)
    return [*kept, f"--disable-extensions-except={joined}", f"--load-extension={joined}"]


def extension_ids(targets: Sequence[dict[str, Any]]) -> set[str]:
    """从 CDP target 列表里抽出已加载的扩展 id。

    扩展的 service worker / background 页会以 ``chrome-extension://<32位id>/…``
    出现。整段 JSON 扫一遍比挑字段稳 —— 不同 Chromium 版本放的字段不一样。
    """
    blob = json.dumps(targets)
    return set(_EXT_ID_RE.findall(blob))


# --------------------------------------------------------------------------- #
# 数据
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Extension:
    """目标机上的一个插件目录。"""

    dirname: str
    container_path: str
    name: str = ""
    version: str = ""
    manifest_version: int = 0
    files: int = 0
    #: 容器里的非 root 进程读不到的条目数。> 0 就是 ``chmod -R a+rX`` 没做
    unreadable: int = 0

    @property
    def mv3(self) -> bool:
        return self.manifest_version >= 3

    @property
    def problems(self) -> list[str]:
        out = []
        if self.manifest_version and not self.mv3:
            out.append(
                f"MV{self.manifest_version}: Chromium 146 已不支持 MV2，这个插件不会被加载"
            )
        if not self.manifest_version:
            out.append("读不到 manifest.json，目录可能没传完")
        if self.unreadable:
            out.append(
                f"{self.unreadable} 个条目对其他用户不可读；容器里的浏览器不是 root，"
                "需要 chmod -R a+rX"
            )
        return out

    def __str__(self) -> str:
        head = f"{self.dirname}  {self.name or '?'} {self.version} MV{self.manifest_version or '?'}"
        return f"{head}  ({self.files} 文件)"


@dataclass(frozen=True, slots=True)
class ProfileChange:
    """一个 profile 的 ``launch_args`` 改了什么。"""

    id: str
    name: str
    before: list[str]
    after: list[str]
    updated: bool
    stopped: bool = False

    @property
    def summary(self) -> str:
        if not self.updated:
            return f"{self.name or self.id}: 已经是目标状态"
        gained = set(extension_paths(self.after)) - set(extension_paths(self.before))
        lost = set(extension_paths(self.before)) - set(extension_paths(self.after))
        bits = []
        if gained:
            bits.append("+" + ",".join(sorted(p.rsplit("/", 1)[-1] for p in gained)))
        if lost:
            bits.append("-" + ",".join(sorted(p.rsplit("/", 1)[-1] for p in lost)))
        tail = "，已停止待重启" if self.stopped else ""
        return f"{self.name or self.id}: {' '.join(bits) or '参数重排'}{tail}"


@dataclass(frozen=True, slots=True)
class VerifyReport:
    """一个 profile 上"扩展到底加载了没有"的结论。"""

    id: str
    name: str
    running: bool
    loaded: set[str] = field(default_factory=set)
    expected: int = 0

    @property
    def ok(self) -> bool:
        return self.running and len(self.loaded) >= self.expected > 0

    @property
    def summary(self) -> str:
        if not self.running:
            return f"{self.name or self.id}: 没有运行，无法验证"
        if not self.expected:
            return f"{self.name or self.id}: 没配置扩展"
        got = len(self.loaded)
        mark = "✓" if self.ok else "✗"
        return f"{mark} {self.name or self.id}: 加载了 {got}/{self.expected} 个扩展"


# --------------------------------------------------------------------------- #
# 插件运维
# --------------------------------------------------------------------------- #


class ExtensionOps:
    """插件的推送、下发与验证。

    同时需要**文件通道**（往目标机传目录）和**HTTP 通道**（改 profile），所以它建在
    :class:`~sleight.deploy.engine.Deployer` 上 —— Runner 和 Manager 客户端都从那里来。

    :param deployer: 目标部署
    :param on_progress: 每完成一步回调一次
    """

    def __init__(self, deployer: Deployer, *, on_progress: Callable[[str], None] | None = None) -> None:
        self.dep = deployer
        self.spec = deployer.spec
        self.runner = deployer.runner
        self._on_progress = on_progress or deployer.say

    def _say(self, message: str) -> None:
        self._on_progress(message)

    # ------------------------------------------------------------------ #
    # 看
    # ------------------------------------------------------------------ #

    def list_installed(self) -> list[Extension]:
        """目标机上装了哪些插件，以及它们健不健康。

        ``manifest.json`` 用 ``cat`` 取回来在**控制机上**解析 —— 目标机上不一定有
        python，容器里那个也不该为了看一眼 manifest 就 exec 进去。
        """
        listing = self.runner.run(
            ["sh", "-c", f"ls -1 {_q(self.spec.extensions_dir)} 2>/dev/null || true"]
        )
        out: list[Extension] = []
        for dirname in sorted(filter(None, (ln.strip() for ln in listing.out.splitlines()))):
            host = f"{self.spec.extensions_dir}/{dirname}"
            if not self.runner.run(["test", "-d", host]).ok:
                continue
            out.append(self._inspect(dirname, host))
        return out

    def _inspect(self, dirname: str, host_path: str) -> Extension:
        manifest = self.runner.read_text(f"{host_path}/manifest.json", sudo=self.dep.sudo) or ""
        data = _parse_manifest(manifest)
        files = self.runner.run(
            ["sh", "-c", f"find {_q(host_path)} -type f | wc -l"]
        )
        # 目录要 o+x、文件要 o+r，否则容器里的非 root 浏览器进程读不到
        bad = self.runner.run([
            "sh", "-c",
            f"find {_q(host_path)} \\( -type d ! -perm -o+x -o -type f ! -perm -o+r \\) | wc -l",
        ])
        return Extension(
            dirname=dirname,
            container_path=f"{self.spec.container_extensions_dir}/{dirname}",
            name=str(data.get("name", "")),
            version=str(data.get("version", "")),
            manifest_version=int(data.get("manifest_version") or 0),
            files=_int(files.text),
            unreadable=_int(bad.text),
        )

    # ------------------------------------------------------------------ #
    # 推
    # ------------------------------------------------------------------ #

    def push(self, local_dir: str | Path, *, name: str | None = None) -> Extension:
        """把本地插件目录推到目标机。

        顺序是刻意的：**先在本地校验 MV3**，不合格根本不传 —— 传一个 Chromium 146
        不会加载的 MV2 插件毫无意义。传完 ``chmod -R a+rX``，再从容器里读一次
        ``manifest.json`` 确认挂载和权限都对。

        :param local_dir: 本地插件目录（里面要有 ``manifest.json``）
        :param name: 目标机上的目录名，默认用本地目录名
        :returns: 目标机上的 :class:`Extension`
        :raises DeployError: 本地目录不合法，或不是 MV3，或容器读不到
        """
        src = Path(local_dir).expanduser().resolve()
        manifest = src / "manifest.json"
        if not manifest.is_file():
            raise DeployError(f"{src} has no manifest.json — that is not an extension directory")
        local = _parse_manifest(manifest.read_text(encoding="utf-8", errors="replace"))
        mv = int(local.get("manifest_version") or 0)
        if mv < 3:
            raise DeployError(
                f"{src.name} declares manifest_version={mv or '?'}; Chromium 146 dropped MV2 "
                "support, so this extension would silently never load. Ship an MV3 build."
            )

        dirname = name or src.name
        host = f"{self.spec.extensions_dir}/{dirname}"
        self.dep.mutate(["mkdir", "-p", self.spec.extensions_dir], sudo=self.dep.sudo)
        self._say(f"传 {src} → {self.runner.label}:{host}")
        if not self.dep.dry_run:
            self.runner.put_dir(str(src), host, sudo=self.dep.sudo)
        # 容器里的浏览器进程不是 root；a+rX 给所有人读、目录给进入权限，不给文件加执行位
        self.dep.mutate(["chmod", "-R", "a+rX", self.spec.extensions_dir], sudo=self.dep.sudo)

        if self.dep.dry_run:
            return Extension(
                dirname, f"{self.spec.container_extensions_dir}/{dirname}",
                str(local.get("name", "")), str(local.get("version", "")), mv,
            )

        ext = self._inspect(dirname, host)
        inside = self.dep.probe([
            "docker", "exec", self.spec.container_name,
            "cat", f"{ext.container_path}/manifest.json",
        ])
        if not inside.ok:
            raise DeployError(
                f"the manager container cannot read {ext.container_path}/manifest.json — "
                f"check the mount: docker inspect {self.spec.container_name} "
                "--format '{{range .Mounts}}{{.Source}} -> {{.Destination}}\\n{{end}}'"
            )
        self._say(f"已装 {ext}")
        for problem in ext.problems:
            self._say(f"! {problem}")
        return ext

    def remove(self, dirname: str) -> None:
        """删掉目标机上的一个插件目录。``apply()`` 之后才会真的从 profile 上摘掉。"""
        host = f"{self.spec.extensions_dir}/{dirname}"
        if not self.runner.run(["test", "-d", host]).ok:
            raise NotFound(f"no extension directory {dirname!r} on {self.runner.label}")
        self._say(f"删除 {host}")
        self.dep.mutate(["rm", "-rf", host], sudo=self.dep.sudo).check()

    # ------------------------------------------------------------------ #
    # 下发
    # ------------------------------------------------------------------ #

    def apply(
        self,
        *,
        names: Sequence[str] | None = None,
        restart: bool = True,
        token: str | None = None,
    ) -> list[ProfileChange]:
        """把插件配置同步到**每一个** profile，然后停掉在跑的实例。

        :param names: 只启用这些插件目录（顺序即参数里的顺序）。``None`` = 目标机上
            装了的全部
        :param restart: 停掉在运行的实例，让新参数在下次 launch 时生效。
            ``False`` 则只改配置 —— 那些实例会继续用旧参数跑
        :param token: Manager token，不给就从目标机的 ``.env`` 读
        :returns: 每个 profile 的改动
        :raises DeployError: 指定的插件目录在目标机上不存在
        """
        installed = {e.dirname: e for e in self.list_installed()}
        chosen = list(names) if names is not None else sorted(installed)
        missing = [n for n in chosen if n not in installed]
        if missing:
            raise DeployError(
                f"not installed on {self.runner.label}: {', '.join(missing)} "
                f"(have: {', '.join(sorted(installed)) or 'nothing'}) — push them first"
            )
        for name in chosen:
            for problem in installed[name].problems:
                self._say(f"! {name}: {problem}")

        paths = [installed[n].container_path for n in chosen]
        self._say(f"目标扩展路径：{','.join(paths) or '（无，将摘掉全部插件）'}")

        changes: list[ProfileChange] = []
        with self.dep.connect(token=token) as mgr:
            profiles = mgr.list_profiles()
            if not profiles:
                self._say("这个 Manager 上还没有 profile")
            for raw in profiles:
                changes.append(self._apply_one(mgr, raw, paths, restart=restart))
        return changes

    def _apply_one(
        self, mgr: CloakBrowserManager, raw: dict[str, Any], paths: list[str], *, restart: bool
    ) -> ProfileChange:
        pid = str(raw.get("id", ""))
        name = str(raw.get("name") or "")
        before = [str(a) for a in (raw.get("launch_args") or [])]
        after = merge_launch_args(before, paths)
        if after == before:
            return ProfileChange(pid, name, before, after, updated=False)

        if not self.dep.dry_run:
            mgr.update_profile(pid, launch_args=after)
        stopped = False
        if restart and raw.get("status") == "running":
            # 参数只在进程启动时读，所以必须停一次。不用手动拉起 ——
            # 下次 lease() 时 ensure_ready() 会自动 launch。
            if not self.dep.dry_run:
                mgr.stop(pid)
            stopped = True
        change = ProfileChange(pid, name, before, after, updated=True, stopped=stopped)
        self._say(change.summary)
        return change

    # ------------------------------------------------------------------ #
    # 验
    # ------------------------------------------------------------------ #

    def verify(
        self,
        *,
        launch: bool = True,
        settle: float = 6.0,
        token: str | None = None,
    ) -> list[VerifyReport]:
        """决定性检查：浏览器里**真的**有那些扩展吗。

        不用跑爬虫。加载成功的扩展会以 ``chrome-extension://<32位id>`` 出现在
        ``/cdp/json/list`` 里。MV3 的 service worker 起来要几秒，所以要等一下再查。

        :param launch: 顺便把停着的实例拉起来（要看到扩展就得让浏览器先跑起来）
        :param settle: 拉起后等多久再查，秒
        :param token: Manager token，不给就从目标机的 ``.env`` 读
        :returns: 每个 profile 的结论
        """
        reports: list[VerifyReport] = []
        with self.dep.connect(token=token) as mgr:
            for raw in mgr.list_profiles():
                pid = str(raw.get("id", ""))
                name = str(raw.get("name") or "")
                expected = len(extension_paths([str(a) for a in (raw.get("launch_args") or [])]))
                running = raw.get("status") == "running"
                if not running and launch:
                    self._say(f"拉起 {name or pid}")
                    mgr.ensure_ready(pid)
                    running = True
                if not running:
                    reports.append(VerifyReport(pid, name, False, expected=expected))
                    continue
                loaded = self._poll_targets(mgr, pid, expected=expected, settle=settle)
                report = VerifyReport(pid, name, True, loaded, expected)
                self._say(report.summary)
                reports.append(report)
        return reports

    def _poll_targets(
        self, mgr: CloakBrowserManager, pid: str, *, expected: int, settle: float
    ) -> set[str]:
        """轮询而不是睡一个固定时长 —— service worker 的启动时间跟机器负载有关。"""
        if expected == 0:
            return extension_ids(mgr.cdp_targets(pid))
        deadline = time.monotonic() + max(settle, 1.0)
        while True:
            loaded = extension_ids(mgr.cdp_targets(pid))
            if len(loaded) >= expected or time.monotonic() >= deadline:
                return loaded
            time.sleep(1.0)

    # ------------------------------------------------------------------ #
    # 漂移
    # ------------------------------------------------------------------ #

    def drift(self, *, token: str | None = None) -> dict[str, Any]:
        """磁盘上装了什么 vs 每个 profile 配了什么。

        专门抓那个最难查的症状：**只改了一部分 profile**。``lease()`` 随机租实例，
        漏掉一个就是概率性失败 —— "有时候能过付费墙，有时候不能"。

        :returns: ``{"installed", "profiles", "consistent"}``
        """
        installed = self.list_installed()
        expected = {e.container_path for e in installed}
        rows: list[dict[str, Any]] = []
        with self.dep.connect(token=token) as mgr:
            for raw in mgr.list_profiles():
                configured = set(extension_paths([str(a) for a in (raw.get("launch_args") or [])]))
                rows.append({
                    "id": raw.get("id"),
                    "name": raw.get("name") or "",
                    "status": raw.get("status"),
                    "configured": sorted(configured),
                    "missing": sorted(expected - configured),
                    "unknown": sorted(configured - expected),
                })
        consistent = not any(r["missing"] or r["unknown"] for r in rows)
        return {
            "installed": [asdict(e) for e in installed],
            "profiles": rows,
            "consistent": consistent,
        }


# --------------------------------------------------------------------------- #
# profile 运维
# --------------------------------------------------------------------------- #


class ProfileOps:
    """profile 的查、启、停。

    薄薄一层 —— 真正的逻辑早在 :class:`~sleight.providers.CloakBrowserManager` 里了，
    这里只负责"远程时自动开隧道"和"停全部"这种批量动作。
    """

    def __init__(self, deployer: Deployer, *, on_progress: Callable[[str], None] | None = None) -> None:
        self.dep = deployer
        self._on_progress = on_progress or deployer.say

    def list(self, *, token: str | None = None) -> list[dict[str, Any]]:
        with self.dep.connect(token=token) as mgr:
            return mgr.list_profiles()

    def launch(self, ident: str, *, token: str | None = None) -> str:
        """按 id 或名字拉起一个实例。

        :returns: profile id
        :raises NotFound: 找不到
        """
        with self.dep.connect(token=token) as mgr:
            pid = _resolve(mgr, ident)
            mgr.ensure_ready(pid)
            self._on_progress(f"{ident} 已就绪")
            return pid

    def stop(self, ident: str, *, token: str | None = None) -> str:
        with self.dep.connect(token=token) as mgr:
            pid = _resolve(mgr, ident)
            mgr.stop(pid)
            self._on_progress(f"{ident} 已停止")
            return pid

    def stop_all(self, *, token: str | None = None) -> list[str]:
        """停掉全部在跑的实例。

        改完 ``launch_args`` 或换过插件文件后必须做一次 —— 两者都只在浏览器进程启动时
        读。**不会丢登录态**：那些在 ``/data/profiles/<id>/`` 里，跟着 ``/data`` 一起
        持久化。真正会丢的是 ``DELETE /api/profiles/{id}``。

        :returns: 被停掉的 profile id
        """
        stopped: list[str] = []
        with self.dep.connect(token=token) as mgr:
            for raw in mgr.list_profiles():
                pid = str(raw.get("id", ""))
                if mgr.status(pid) is not InstanceStatus.RUNNING:
                    continue
                mgr.stop(pid)
                stopped.append(pid)
                self._on_progress(f"停 {raw.get('name') or pid}")
        return stopped


# --------------------------------------------------------------------------- #


def _resolve(mgr: CloakBrowserManager, ident: str) -> str:
    """id 或名字 → id。名字比 id 好记，两个都收。"""
    if mgr.status(ident) is not InstanceStatus.NOT_FOUND:
        return ident
    found = mgr.find_profile(ident)
    if found is None:
        raise NotFound(f"{mgr.name}: no profile with id or name {ident!r}")
    return str(found["id"])


def _parse_manifest(text: str) -> dict[str, Any]:
    try:
        data = json.loads(text)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _int(text: str) -> int:
    try:
        return int(text.strip().split()[0])
    except (ValueError, IndexError):
        return 0


def _q(path: str) -> str:
    return shlex.quote(path)
