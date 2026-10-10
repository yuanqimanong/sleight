"""Detect browser capabilities without launching a user's Windows browser."""

from __future__ import annotations

import ctypes
import platform
import re
import subprocess
from ctypes import wintypes
from functools import lru_cache
from pathlib import Path


def _windows_version(path):
    version = ctypes.windll.version
    version.GetFileVersionInfoSizeW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
    version.GetFileVersionInfoSizeW.restype = wintypes.DWORD
    version.GetFileVersionInfoW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
    version.GetFileVersionInfoW.restype = wintypes.BOOL
    version.VerQueryValueW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.UINT)]
    version.VerQueryValueW.restype = wintypes.BOOL
    size = version.GetFileVersionInfoSizeW(str(path), None)
    if not size:
        return "", ""
    buffer = ctypes.create_string_buffer(size)
    if not version.GetFileVersionInfoW(str(path), 0, size, buffer):
        return "", ""

    def query(key):
        pointer, length = ctypes.c_void_p(), ctypes.c_uint()
        if version.VerQueryValueW(buffer, key, ctypes.byref(pointer), ctypes.byref(length)):
            return ctypes.wstring_at(pointer.value, max(0, length.value - 1))
        return ""

    pointer, length = ctypes.c_void_p(), wintypes.UINT()
    translations = ["040904b0", "040904e4"]
    if version.VerQueryValueW(buffer, r"\VarFileInfo\Translation", ctypes.byref(pointer), ctypes.byref(length)):
        pairs = ctypes.cast(pointer, ctypes.POINTER(ctypes.c_ushort))
        translations = [f"{pairs[i]:04x}{pairs[i+1]:04x}" for i in range(0, length.value // 2 - 1, 2)] + translations
    for translation in translations:
        prefix = "\\StringFileInfo\\" + translation + "\\"
        value, product = query(prefix + "ProductVersion"), query(prefix + "ProductName")
        if value:
            return value, product
    return "", ""


@lru_cache(maxsize=256)
def _version(binary, modified):
    try:
        if platform.system() == "Windows":
            return _windows_version(binary)
        result = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=3, check=False)
        text = result.stdout.strip()[:200]
        match = re.search(r"\d+\.\d+[\d.]*", text)
        return match[0] if match else "", text
    except (OSError, subprocess.SubprocessError, AttributeError):
        return "", ""


def browser_capabilities(binary: str, name: str = "") -> dict:
    path = Path(binary)
    version, product = _version(binary, path.stat().st_mtime_ns) if path.is_file() else ("", "")
    identity = (name + " " + binary.replace("\\", "/") + " " + product).lower()
    fingerprint = "fingerprint-chromium" in identity
    testing = "chrome-for-testing" in identity or "chrome for testing" in identity
    edge = "edge" in identity or "msedge" in identity
    branded = "google/chrome" in identity or "google chrome" in identity or name.lower() == "chrome"
    chromium = "chromium" in identity
    major = int(version.split(".")[0]) if version and version.split(".")[0].isdigit() else None
    automatic = fingerprint or testing or chromium or (branded and major is not None and major < 137)
    if edge:
        automatic = False
    return {"kind": "fingerprint" if fingerprint else "native", "fingerprint": fingerprint,
            "version": version, "extensions": "automatic" if automatic else "manual" if branded or edge else "unknown",
            "extension_note": "支持自动加载解包插件" if automatic else "可在实例窗口中手动安装；自动加载受限" if branded or edge else "未确认自动加载能力，请先验证内核",
            "name": name or product or path.name}
