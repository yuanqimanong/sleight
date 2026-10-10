"""Public capabilities, screenshot attachment and registry cache behavior."""

import base64
import time
from contextlib import contextmanager

import pytest

from sleight.runtime import capabilities, inspector
from sleight.service import registry


@pytest.mark.parametrize("name,binary,version,product,expected", [
    ("Chrome", "Chrome/chrome.exe", "137.0", "Google Chrome", "manual"),
    ("Chrome", "Chrome/chrome.exe", "136.0", "Google Chrome", "automatic"),
    ("Edge", "msedge.exe", "136.0", "Microsoft Edge", "manual"),
    ("Chrome", "testing/chrome.exe", "155.0", "Google Chrome for Testing", "automatic"),
    ("fingerprint-chromium", "fp/chrome.exe", "155.0", "Chromium", "automatic"),
])
def test_kernel_capabilities(tmp_path, monkeypatch, name, binary, version, product, expected):
    path = tmp_path / binary
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()
    monkeypatch.setattr(capabilities, "_version", lambda *args: (version, product))
    result = capabilities.browser_capabilities(str(path), name)
    assert result["extensions"] == expected
    assert result["fingerprint"] == (name == "fingerprint-chromium")


def test_inspector_detaches_and_rejects_unsafe_urls(monkeypatch):
    closed = []
    class Transport:
        def call(self, method, params=None):
            assert method == "Target.getTargets"
            return {"targetInfos": [{"targetId": "a", "type": "page", "title": "A"}, {"targetId": "b", "type": "page", "title": "B"}]}
    @contextmanager
    def attach(transport, target, **kw):
        class Session:
            def call(self, method, params):
                assert method == "Page.captureScreenshot"
                return {"data": base64.b64encode(b"jpeg-fixture").decode()}
        yield Session()
        closed.append(target)
    monkeypatch.setattr(inspector.Session, "attach", attach)
    assert inspector.inspect_browser(Transport(), target="b")["target"] == "b"
    assert closed == ["b"]
    for value in ("file:///etc/passwd", "javascript://alert(1)", "https://name:password@example.org"):
        with pytest.raises(ValueError):
            inspector.navigation_url(value)
    assert inspector.navigation_url("example.com") == "https://example.com"


def test_registry_cache_and_offline_fallback(service_db, monkeypatch):
    requests = []
    class Client:
        def __init__(self, **kw):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def get(self, url):
            requests.append(url)
            class Response:
                def raise_for_status(self):
                    pass
                def json(self):
                    return {"results": [{"name": "v0.1.6", "digest": "sha256:fixture", "images": [{"architecture": "amd64"}, {"architecture": "unknown"}], "last_updated": "2026-10-08"}], "next": None}
            return Response()
    monkeypatch.setattr(registry.httpx, "Client", Client)
    data = registry.manager_images(refresh=True)
    assert data["images"][0]["architectures"] == ["amd64"] and data["images"][0]["verified"]
    assert registry.manager_images()["cached"] and len(requests) == 1
    monkeypatch.setattr(Client, "get", lambda *args: (_ for _ in ()).throw(OSError("offline")))
    fallback = registry.manager_images(refresh=True)
    assert fallback["images"] == data["images"] and fallback["warning"]


@pytest.mark.parametrize("refresh", [False, True])
def test_registry_cache_rechecks_verified_version_after_upgrade(service_db, monkeypatch, refresh):
    service_db.put("settings", "manager-images", {"fetched_at": time.time(), "images": [
        {"image": "cloakhq/cloakbrowser-manager:v0.1.5", "verified": True},
        {"image": registry.DEFAULT_IMAGE, "verified": False},
    ]})
    monkeypatch.setattr(registry.httpx, "Client", lambda **kwargs: (_ for _ in ()).throw(OSError("offline")))
    images = registry.manager_images(refresh=refresh)["images"]
    assert [image["verified"] for image in images] == [False, True]
