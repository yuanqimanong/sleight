"""浏览器内核安装：不联网、不下真 binary。

用假的 GitHub API 响应 + 本地造的 tar.xz，验证选资产、下载、解包、找可执行文件、写
manifest、幂等、卸载，以及 LocalLauncher 用内核名解析到安装缓存。
"""

from __future__ import annotations

import io
import json
import os
import tarfile
from pathlib import Path

import pytest

from sleight import browsers
from sleight.core.errors import SleightError


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    """把安装根目录隔到 tmp，别碰真的 ~/.sleight。"""
    monkeypatch.setenv("SLEIGHT_HOME", str(tmp_path))
    return tmp_path


def _fake_tar_xz() -> bytes:
    """造一个内含 ``pkg/chrome`` 的 tar.xz。"""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:xz") as tar:
        data = b"#!/bin/sh\necho fake chrome\n"
        info = tarfile.TarInfo("pkg/chrome")
        info.size = len(data)
        info.mode = 0o644
        tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


RELEASE = {
    "tag_name": "148.0.7778.215",
    "assets": [
        {"name": "ungoogled-chromium-148-1-x86_64_linux.tar.xz",
         "browser_download_url": "https://example.invalid/linux.tar.xz", "size": 10},
        {"name": "ungoogled-chromium_148_windows_x64.zip",
         "browser_download_url": "https://example.invalid/win.zip", "size": 10},
    ],
}


@pytest.fixture
def offline(monkeypatch):
    """把 release 查询和下载都换成本地假货。"""
    payload = _fake_tar_xz()
    monkeypatch.setattr(browsers, "_latest_release", lambda kernel: RELEASE)
    monkeypatch.setattr(browsers, "_release_by_tag", lambda kernel, tag: RELEASE)
    monkeypatch.setattr(browsers, "_this_platform", lambda: "linux")
    monkeypatch.setattr(browsers.platform, "machine", lambda: "x86_64")

    def fake_download(url, dest, on_progress):
        Path(dest).write_bytes(payload)
        if on_progress:
            on_progress(len(payload), len(payload))
        return "deadbeef"

    monkeypatch.setattr(browsers, "_download", fake_download)
    return payload


def test_install_downloads_unpacks_and_records_a_manifest(offline, home):
    path = browsers.install("fingerprint-chromium")
    assert path.is_file() and path.name == "chrome"
    # 执行位只在 POSIX 上有意义。Windows 没有这个概念（install 本身也只在非 Windows chmod），
    # 而这套用例是把平台 mock 成 linux 跑的，真跑在 Windows 时不能断言它。
    if os.name != "nt":
        assert path.stat().st_mode & 0o111, "解包后应可执行"

    manifest = json.loads((path.parents[1] / "manifest.json").read_text())
    assert manifest["kernel"] == "fingerprint-chromium"
    assert manifest["version"] == "148.0.7778.215"
    assert manifest["sha256"] == "deadbeef"
    assert manifest["executable"] == str(path)
    # 归档解包后就删掉，别占几百 MB
    assert not list(path.parents[1].glob("*.tar.xz"))


def test_install_is_idempotent(offline, monkeypatch):
    first = browsers.install("fingerprint-chromium")
    calls = []
    monkeypatch.setattr(browsers, "_download",
                        lambda *a, **k: calls.append(1) or "x")
    again = browsers.install("fingerprint-chromium")
    assert again == first and not calls, "装过就直接返回，不该重新下载"


def test_installed_and_path_and_uninstall(offline):
    browsers.install("fingerprint-chromium")
    entries = browsers.installed("fingerprint-chromium")
    assert len(entries) == 1 and entries[0]["version"] == "148.0.7778.215"
    assert browsers.installed_path("fingerprint-chromium") is not None

    removed = browsers.uninstall("fingerprint-chromium")
    assert removed == ["148.0.7778.215"]
    assert browsers.installed_path("fingerprint-chromium") is None


def test_unknown_kernel_is_rejected():
    with pytest.raises(SleightError, match="unknown browser kernel"):
        browsers.install("netscape-navigator")


def test_missing_asset_for_platform_is_reported(offline, monkeypatch):
    monkeypatch.setattr(browsers, "_this_platform", lambda: "macos")
    with pytest.raises(SleightError, match="no macos asset"):
        browsers.install("fingerprint-chromium")


def test_non_x86_64_is_refused(offline, monkeypatch):
    monkeypatch.setattr(browsers.platform, "machine", lambda: "arm64")
    with pytest.raises(SleightError, match="only ships x86_64"):
        browsers.install("fingerprint-chromium")


def test_resolve_maps_kernel_name_to_installed_path(offline):
    assert browsers.resolve("fingerprint-chromium") is None      # 没装
    path = browsers.install("fingerprint-chromium")
    assert browsers.resolve("fingerprint-chromium") == str(path)
    assert browsers.resolve("chromium") is None                  # 不是已知内核名


def test_local_launcher_resolves_an_installed_kernel_by_name(offline):
    from sleight.providers.local import LocalLauncher

    path = browsers.install("fingerprint-chromium")
    prov = LocalLauncher("fingerprint-chromium")
    assert prov._binary == str(path), "LocalLauncher 应能用内核名命中安装缓存"


def test_local_launcher_says_how_to_install_a_missing_kernel():
    from sleight.core.errors import InstanceError
    from sleight.providers.local import LocalLauncher

    with pytest.raises(InstanceError, match="sleight browser install"):
        LocalLauncher("fingerprint-chromium")


def test_zip_member_escaping_the_target_is_refused(tmp_path):
    import zipfile

    archive = tmp_path / "evil.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("../escaped.txt", "nope")
    with pytest.raises(SleightError, match="escaping"):
        browsers._unpack(archive, tmp_path / "out", "linux")
