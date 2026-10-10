"""Official Docker Hub tags, with bounded cache and manual fallback."""

import time

import httpx

from ..deploy.spec import DEFAULT_IMAGE
from .database import database


def manager_images(*, refresh=False):
    db = database()
    cached = db.get("settings", "manager-images", {})
    if "images" in cached:
        cached = {**cached, "images": [
            {**image, "verified": image.get("image") == DEFAULT_IMAGE}
            for image in cached["images"]
        ]}
    if not refresh and cached.get("fetched_at", 0) > time.time() - 3600:
        return {**cached, "cached": True}
    result = []
    try:
        with httpx.Client(timeout=10, follow_redirects=False) as client:
            url = "https://hub.docker.com/v2/repositories/cloakhq/cloakbrowser-manager/tags?page_size=100"
            for _ in range(3):
                response = client.get(url)
                response.raise_for_status()
                payload = response.json()
                for tag in payload["results"]:
                    name = tag["name"]
                    if name == "latest" or not name.startswith("v"):
                        continue
                    result.append({"tag": name, "image": "cloakhq/cloakbrowser-manager:" + name,
                                   "digest": tag.get("digest", ""), "updated": tag.get("last_updated", ""),
                                   "architectures": sorted({r["architecture"] for r in tag.get("images", []) if r.get("architecture") not in (None, "unknown")}),
                                   "verified": "cloakhq/cloakbrowser-manager:" + name == DEFAULT_IMAGE})
                url = payload.get("next")
                if not url or not url.startswith("https://hub.docker.com/v2/repositories/cloakhq/cloakbrowser-manager/tags?"):
                    break
        value = {"images": result, "fetched_at": time.time(), "cached": False, "warning": ""}
        db.put("settings", "manager-images", value)
        return value
    except Exception:
        return {"images": cached.get("images", [{"tag": DEFAULT_IMAGE.split(":")[-1], "image": DEFAULT_IMAGE, "verified": True, "architectures": [], "digest": "", "updated": ""}]),
                "fetched_at": cached.get("fetched_at", 0), "cached": True, "warning": "官方版本列表暂不可用，使用缓存或手动填写镜像版本"}
