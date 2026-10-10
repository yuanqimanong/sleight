"""Manager wire compatibility. Configuration intent is validated before serialization."""

from __future__ import annotations

from typing import Any

MODERN_FIELDS = frozenset({
    "name", "fingerprint_seed", "proxy", "timezone", "locale", "screen_width",
    "screen_height", "gpu_family", "humanize", "human_preset", "geoip",
    "clipboard_sync", "auto_launch", "color_scheme", "launch_args", "extension_paths",
    "allow_3p_cookies", "set_google_default", "capture_preview", "restore_session",
    "notes", "tags",
})
LEGACY_ONLY = frozenset({"platform", "gpu_vendor", "gpu_renderer", "headless",
                         "user_agent", "hardware_concurrency"})


def extension_list(payload: dict[str, Any]) -> list[str]:
    paths = list(payload.get("extension_paths") or [])
    for arg in payload.get("launch_args") or []:
        if str(arg).startswith("--load-extension="):
            paths.extend(str(arg).split("=", 1)[1].split(","))
    return list(dict.fromkeys(str(p).strip() for p in paths if str(p).strip()))


def adapt_payload(payload: dict[str, Any], *, modern: bool, host_os: str = "linux") -> dict[str, Any]:
    body = dict(payload)
    paths = extension_list(body)
    args = [a for a in body.get("launch_args", []) if not str(a).startswith(
        ("--load-extension=", "--disable-extensions-except="))]
    if modern:
        persona = "macos" if host_os == "macos" else "windows"
        if body.get("platform", persona) != persona:
            raise ValueError(f"This Manager uses a {persona} persona on {host_os}; "
                             f"platform={body['platform']!r} cannot be preserved")
        if body.get("headless"):
            raise ValueError("Manager 0.1.x profiles are headed; headless=True is unsupported")
        if body.get("user_agent") or body.get("hardware_concurrency"):
            raise ValueError("Manager 0.1.x derives user_agent and hardware_concurrency from its seed")
        family = body.get("gpu_family")
        if not family and ("gpu_vendor" in body or "gpu_renderer" in body):
            gpu = f"{body.get('gpu_vendor', '')} {body.get('gpu_renderer', '')}".lower()
            if gpu and not any(g in gpu for g in ("intel", "nvidia", "apple")):
                raise ValueError("Manager 0.1.x cannot preserve this GPU renderer; use gpu_family")
            family = "intel" if "intel" in gpu else "nvidia" if "nvidia" in gpu else "auto"
        body = {k: v for k, v in body.items() if k in MODERN_FIELDS}
        if family is not None:
            body["gpu_family"] = family
        if "launch_args" in payload:
            body["launch_args"] = args
        if "extension_paths" in payload or paths:
            body["extension_paths"] = paths
    else:
        if body.get("gpu_family") not in (None, "auto"):
            raise ValueError("Legacy Manager needs gpu_vendor/gpu_renderer instead of gpu_family")
        for key in ("allow_3p_cookies", "set_google_default", "capture_preview", "restore_session"):
            if key in body:
                raise ValueError(f"{key} requires Manager 0.1.x")
        for key in MODERN_FIELDS - {"name", "fingerprint_seed", "proxy", "timezone", "locale",
                                    "screen_width", "screen_height", "humanize", "human_preset",
                                    "geoip", "clipboard_sync", "auto_launch", "color_scheme",
                                    "launch_args", "notes", "tags"}:
            body.pop(key, None)
        if paths:
            joined = ",".join(paths)
            args.extend([f"--load-extension={joined}", f"--disable-extensions-except={joined}"])
        if "launch_args" in payload or "extension_paths" in payload:
            body["launch_args"] = args
    return body
