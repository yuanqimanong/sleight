"""下载并安装浏览器内核 —— 类似 ``playwright install``。

指纹内核（fingerprint-chromium 等）是第三方预编译产物，不进 PyPI 包（体积上百 MB，也不该
由本库再分发）。这里做的是 playwright 那套：**按平台挑资产 → 下载到本地缓存 → 解包 → 返回
可执行文件路径**，幂等，装过就直接返回。

    sleight browser install fingerprint-chromium     # CLI
    from sleight.browsers import install; install("fingerprint-chromium")   # API

装完之后 :class:`~sleight.providers.local.LocalLauncher` / :func:`sleight.launch` 直接用内核
名字就行（``launch("fingerprint-chromium", fingerprint=42)``），不必写绝对路径。

**供应链提醒（诚实说明）：** 这是从第三方 GitHub Release 下载并运行一个浏览器二进制。
上游没有发布签名校验和，所以本模块**只能**记录"我们下载到的东西"的 SHA-256 到
``manifest.json`` 供你审计/复核，不能证明它由对应源码构建。介意的话就别用这条路，
自己拿 binary 传绝对路径给 LocalLauncher。
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import stat
import subprocess
import tarfile
import urllib.request
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .core.errors import SleightError
from .deploy.inventory import sleight_home

__all__ = ["KERNELS", "Kernel", "install", "installed", "installed_path", "uninstall"]


@dataclass(frozen=True)
class Kernel:
    """一个可安装的浏览器内核。

    :param name: 内核名（CLI/API 用它）
    :param repo: GitHub ``owner/repo``，用来查 Release
    :param assets: ``平台 -> 资产名匹配子串``；平台是 ``linux`` / ``windows`` / ``macos``
    :param note: 一句话说明，列出来给用户看
    """

    name: str
    repo: str
    assets: dict[str, str]
    note: str


#: 已知内核。v1 只收一个 —— 调研结论里唯一同时满足「开源 + C++ 源码级指纹 + 保留 CDP +
#: seed 稳定人格 + 三平台预编译」的候选。加别的内核只要往这里加一条。
KERNELS: dict[str, Kernel] = {
    "fingerprint-chromium": Kernel(
        name="fingerprint-chromium",
        repo="adryfish/fingerprint-chromium",
        assets={
            "linux": "_linux.tar.xz",
            "windows": "_windows_x64.zip",
            "macos": "_macos.dmg",
        },
        note="ungoogled-chromium + C++ 指纹补丁；--fingerprint=<seed> 给稳定人格；仅 x86_64",
    ),
}

_PLATFORMS = {"Linux": "linux", "Windows": "windows", "Darwin": "macos"}
#: 解包后按这些相对路径找可执行文件（按平台）
_EXE_HINTS = {
    "linux": ("chrome", "chrome-wrapper", "chromium"),
    "windows": ("chrome.exe", "chromium.exe"),
    "macos": ("Contents/MacOS/Chromium", "Contents/MacOS/Google Chrome"),
}


def _this_platform() -> str:
    key = _PLATFORMS.get(platform.system())
    if key is None:
        raise SleightError(f"unsupported platform: {platform.system()}")
    return key


def browsers_home() -> Path:
    """内核安装根目录（``$SLEIGHT_HOME/browsers``，默认 ``./data/browsers``）。"""
    return sleight_home() / "browsers"


def _kernel(name: str) -> Kernel:
    kernel = KERNELS.get(name)
    if kernel is None:
        raise SleightError(f"unknown browser kernel {name!r}; known: {', '.join(KERNELS)}")
    return kernel


def _latest_release(kernel: Kernel) -> dict[str, Any]:
    url = f"https://api.github.com/repos/{kernel.repo}/releases/latest"
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp)
    except Exception as exc:
        raise SleightError(f"could not query releases for {kernel.repo}: {exc}") from exc


def _pick_asset(kernel: Kernel, release: dict[str, Any], plat: str) -> dict[str, Any]:
    needle = kernel.assets.get(plat)
    if needle is None:
        raise SleightError(f"{kernel.name} has no {plat} build")
    for asset in release.get("assets") or []:
        if needle in asset.get("name", ""):
            return asset
    have = ", ".join(a.get("name", "?") for a in release.get("assets") or [])
    raise SleightError(f"no {plat} asset matching {needle!r} in {release.get('tag_name')}; got: {have}")


def _download(url: str, dest: Path, on_progress: Callable[[int, int], None] | None) -> str:
    """下载到 ``dest``，返回内容的 SHA-256。"""
    digest = hashlib.sha256()
    req = urllib.request.Request(url, headers={"Accept": "application/octet-stream"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        with open(dest, "wb") as fh:
            while chunk := resp.read(1 << 20):
                fh.write(chunk)
                digest.update(chunk)
                done += len(chunk)
                if on_progress:
                    on_progress(done, total)
    return digest.hexdigest()


def _unpack(archive: Path, into: Path, plat: str) -> None:
    into.mkdir(parents=True, exist_ok=True)
    name = archive.name
    if name.endswith((".tar.xz", ".tar.gz")):
        with tarfile.open(archive) as tar:
            # filter="data" 拒绝绝对路径/越界成员，防归档穿越（Python 3.12+ 默认，显式写更清楚）
            tar.extractall(into, filter="data")
    elif name.endswith(".zip"):
        with zipfile.ZipFile(archive) as zf:
            root = into.resolve()
            links = []
            for member in zf.infolist():
                target = (root / member.filename).resolve()
                if not target.is_relative_to(root):
                    raise SleightError(f"refusing zip member escaping the target dir: {member.filename}")
                mode = member.external_attr >> 16
                if stat.S_ISLNK(mode):
                    link = zf.read(member).decode('utf-8')
                    if os.path.isabs(link) or not (target.parent / link).resolve().is_relative_to(root):
                        raise SleightError(f"refusing zip link escaping the target dir: {member.filename}")
                    links.append((target, link))
                else:
                    zf.extract(member, root)
                    if mode and not member.is_dir():
                        target.chmod(mode & 0o777)
            # macOS .app bundles use framework symlinks. Create after regular files,
            # so archive members can never write through an earlier link.
            for target, link in links:
                target.parent.mkdir(parents=True, exist_ok=True)
                os.symlink(link, target)
    elif name.endswith(".dmg"):
        if plat != "macos":
            raise SleightError("a .dmg can only be unpacked on macOS")
        _unpack_dmg(archive, into)
    else:
        raise SleightError(f"do not know how to unpack {name}")


def _unpack_dmg(archive: Path, into: Path) -> None:
    """挂载 dmg、把里面的 .app 拷出来、卸载。只在 macOS 上可用（要 hdiutil）。"""
    mount = into / "_mnt"
    mount.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["hdiutil", "attach", "-nobrowse", "-quiet", "-mountpoint", str(mount), str(archive)],
        check=True,
    )
    try:
        for app in mount.glob("*.app"):
            shutil.copytree(app, into / app.name, symlinks=True, dirs_exist_ok=True)
    finally:
        subprocess.run(["hdiutil", "detach", "-quiet", str(mount)], check=False)
        shutil.rmtree(mount, ignore_errors=True)


def _find_executable(root: Path, plat: str) -> Path:
    """在解包目录里找到浏览器可执行文件。"""
    hints = _EXE_HINTS[plat]
    if plat == "macos":
        for app in root.rglob("*.app"):
            for hint in hints:
                candidate = app / hint
                if candidate.is_file():
                    return candidate
    for hint in hints:
        for candidate in root.rglob(hint):
            if candidate.is_file():
                return candidate
    raise SleightError(f"unpacked {root}, but found no browser executable (looked for {hints})")


def install_dir(name: str, version: str) -> Path:
    return browsers_home() / name / version


def installed(name: str | None = None) -> list[dict[str, str]]:
    """已装的内核列表：``[{name, version, path}]``。"""
    out: list[dict[str, str]] = []
    root = browsers_home()
    for kernel_dir in sorted(root.iterdir()) if root.is_dir() else []:
        if name and kernel_dir.name != name:
            continue
        for version_dir in sorted(kernel_dir.iterdir()):
            manifest = version_dir / "manifest.json"
            if manifest.is_file():
                data = json.loads(manifest.read_text(encoding="utf-8"))
                out.append({
                    "name": kernel_dir.name, "version": version_dir.name,
                    "path": data.get("executable", ""),
                })
    return out


def installed_path(name: str, version: str | None = None) -> Path | None:
    """已装内核的可执行文件路径；没装返回 ``None``。``version=None`` 取最新装的那个。"""
    entries = [e for e in installed(name) if version is None or e["version"] == version]
    if not entries:
        return None
    path = Path(entries[-1]["path"])
    return path if path.exists() else None


def install(
    name: str = "fingerprint-chromium", *, version: str | None = None, force: bool = False,
    on_progress: Callable[[int, int], None] | None = None,
) -> Path:
    """下载并安装一个浏览器内核，返回可执行文件路径。**幂等** —— 装过就直接返回。

    :param name: :data:`KERNELS` 里的内核名
    :param version: Release tag；``None`` 取上游最新
    :param force: 已装也重装
    :param on_progress: ``(已下载字节, 总字节)`` 回调，给 CLI 显示进度
    :raises SleightError: 平台不支持、找不到资产、下载或解包失败
    """
    kernel = _kernel(name)
    plat = _this_platform()
    if platform.machine().lower() not in ("x86_64", "amd64"):
        raise SleightError(
            f"{name} only ships x86_64 builds; this machine is {platform.machine()}"
        )

    if version and not force and (existing := installed_path(name, version)) is not None:
        return existing

    release = _latest_release(kernel) if version is None else _release_by_tag(kernel, version)
    tag = release.get("tag_name") or "unknown"
    if not force and (existing := installed_path(name, tag)) is not None:
        return existing

    asset = _pick_asset(kernel, release, plat)
    target = install_dir(name, tag)
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True, exist_ok=True)

    archive = target / asset["name"]
    sha256 = _download(asset["browser_download_url"], archive, on_progress)
    try:
        _unpack(archive, target, plat)
    finally:
        archive.unlink(missing_ok=True)          # 解包完就删，省几百 MB

    executable = _find_executable(target, plat)
    if plat != "windows":
        executable.chmod(executable.stat().st_mode | 0o111)

    (target / "manifest.json").write_text(json.dumps({
        "kernel": name, "repo": kernel.repo, "version": tag, "platform": plat,
        "asset": asset["name"], "url": asset["browser_download_url"],
        # 我们下载到的字节的 SHA-256。上游没发布签名校验和，所以这只能证明"以后没被改过"，
        # 不能证明它由对应源码构建 —— 见模块 docstring 的供应链提醒。
        "sha256": sha256, "executable": str(executable),
    }, indent=2), encoding="utf-8")
    return executable


def _release_by_tag(kernel: Kernel, tag: str) -> dict[str, Any]:
    url = f"https://api.github.com/repos/{kernel.repo}/releases/tags/{tag}"
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp)
    except Exception as exc:
        raise SleightError(f"no release {tag!r} in {kernel.repo}: {exc}") from exc


def uninstall(name: str, version: str | None = None) -> list[str]:
    """删掉已装的内核，返回删掉的版本。``version=None`` 删该内核全部版本。"""
    removed: list[str] = []
    for entry in installed(name):
        if version is None or entry["version"] == version:
            shutil.rmtree(install_dir(name, entry["version"]), ignore_errors=True)
            removed.append(entry["version"])
    kernel_dir = browsers_home() / name
    if kernel_dir.is_dir() and not any(kernel_dir.iterdir()):
        kernel_dir.rmdir()
    return removed


def resolve(binary: str) -> str | None:
    """内核名 → 已装的可执行文件路径；不是已知内核名或没装就返回 ``None``。

    给 :class:`~sleight.providers.local.LocalLauncher` 用，让 ``binary="fingerprint-chromium"``
    直接命中安装缓存，不用写绝对路径。
    """
    if binary not in KERNELS:
        return None
    path = installed_path(binary)
    return str(path) if path else None


def _env_override(name: str) -> str | None:
    """``SLEIGHT_BROWSER_<KERNEL>`` 指定路径时优先用它（大写、非字母数字换下划线）。"""
    key = "SLEIGHT_BROWSER_" + "".join(c if c.isalnum() else "_" for c in name).upper()
    return os.environ.get(key)
