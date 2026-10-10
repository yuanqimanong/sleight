"""Bounded tab inspection; attached tabs are detached, never closed."""

import base64
from urllib.parse import urlsplit

from ..core.session import Session


def navigation_url(value):
    value = str(value).strip()
    if value == "about:blank":
        return value
    if "://" not in value:
        value = "https://" + value
    parsed = urlsplit(value)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password or len(value) > 4096:
        raise ValueError("请输入 HTTP(S) 网址，不接受文件、脚本或包含密码的地址")
    return value


def inspect_browser(transport, *, target="", url="", mode="current", screenshot=True):
    pages = [p for p in transport.call("Target.getTargets")["targetInfos"] if p["type"] == "page"][:100]
    selected = next((p for p in pages if p["targetId"] == target), None)
    if not selected and pages:
        selected = pages[0]
    if url:
        url = navigation_url(url)
        if mode not in ("current", "tab", "window"):
            raise ValueError("打开方式不正确")
        if mode != "current" or not selected:
            tid = transport.call("Target.createTarget", {"url": url, "newWindow": mode == "window"})["targetId"]
            selected = {"targetId": tid, "title": "正在打开…", "url": url}
        else:
            with Session.attach(transport, selected["targetId"], track_network=False, track_runtime=False) as session:
                session.call("Page.navigate", {"url": url})
        transport.call("Target.activateTarget", {"targetId": selected["targetId"]})
        return {"target": selected["targetId"], "url": url}
    if not selected:
        return {"pages": [], "target": "", "image": ""}
    image = ""
    if screenshot:
        with Session.attach(transport, selected["targetId"], track_network=False, track_runtime=False) as session:
            data = session.call("Page.captureScreenshot", {"format": "jpeg", "quality": 55, "captureBeyondViewport": False})["data"]
            if len(data) > 12 * 1024 * 1024:
                raise ValueError("页面截图过大，请缩小浏览器窗口")
            base64.b64decode(data, validate=True)
            image = "data:image/jpeg;base64," + data
    return {"pages": [{"targetId": p["targetId"], "title": p.get("title", "")[:200], "url": p.get("url", "")[:4096]} for p in pages],
            "target": selected["targetId"], "image": image}
