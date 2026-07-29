"""网络资源监听。

这一层要防的是三件在真页面上很常见、但用「一个 set 存 URL」的手写实现全都漏掉的事：

1. ``requestWillBeSent`` 的 ``type`` **经常缺失**，只有 ``responseReceived`` 的是权威的
   —— 所以回调的时机必须是"首次满足筛选条件"，不是"首次看见"。
2. 重定向**复用同一个 requestId**，不去处理就会把中间跳的 URL 当成最终结果。
3. ``"StyleSheet"`` 这种大小写拼错的后果是**静默什么都抓不到**。
"""

from __future__ import annotations

import pytest

from sleight.core.protocol import Event
from sleight.core.resources import RESOURCE_TYPES, NetworkResource, ResourceTracker


def sent(request_id: str, url: str, *, type_: str | None = None, method: str = "GET") -> Event:
    params: dict = {"requestId": request_id, "request": {"url": url, "method": method}}
    if type_ is not None:
        params["type"] = type_
    return Event("Network.requestWillBeSent", params)


def redirected(request_id: str, to: str) -> Event:
    """重定向：同一个 requestId 再来一条 requestWillBeSent，带 redirectResponse。"""
    return Event("Network.requestWillBeSent", {
        "requestId": request_id,
        "request": {"url": to, "method": "GET"},
        "redirectResponse": {"status": 302},
    })


def received(request_id: str, *, type_: str, mime: str = "", status: int = 200,
             url: str = "", cached: bool = False) -> Event:
    response: dict = {"status": status, "mimeType": mime, "fromDiskCache": cached}
    if url:
        response["url"] = url
    return Event("Network.responseReceived",
                 {"requestId": request_id, "type": type_, "response": response})


def finished(request_id: str, size: float = 1234.0) -> Event:
    return Event("Network.loadingFinished",
                 {"requestId": request_id, "encodedDataLength": size})


def failed(request_id: str, error: str = "net::ERR_ABORTED", type_: str = "Script") -> Event:
    return Event("Network.loadingFailed",
                 {"requestId": request_id, "errorText": error, "type": type_})


def feed(tracker: ResourceTracker, *events: Event) -> ResourceTracker:
    for ev in events:
        tracker.feed(ev)
    return tracker


# --------------------------------------------------------------------------- #
# 基本收集
# --------------------------------------------------------------------------- #


def test_collects_url_type_and_status():
    t = feed(
        ResourceTracker(),
        sent("1", "https://x/app.js", type_="Script"),
        received("1", type_="Script", mime="application/javascript"),
        finished("1", size=9000),
    )
    (r,) = t.snapshot()
    assert (r.url, r.resource_type, r.method) == ("https://x/app.js", "Script", "GET")
    assert (r.mime_type, r.status, r.size) == ("application/javascript", 200, 9000)
    assert r.finished and not r.failed and not r.pending


def test_a_request_without_a_response_stays_pending():
    t = feed(ResourceTracker(), sent("1", "https://x/slow", type_="XHR"))
    (r,) = t.snapshot()
    assert r.pending and r.status is None and r.size is None


def test_failed_requests_are_kept_with_their_reason():
    """失败的请求也是信息 —— 被广告拦截规则掐掉的脚本正是要查的东西。"""
    t = feed(
        ResourceTracker(),
        sent("1", "https://x/tracker.js", type_="Script"),
        failed("1", "net::ERR_BLOCKED_BY_CLIENT"),
    )
    (r,) = t.snapshot()
    assert r.failed and not r.pending
    assert r.error_text == "net::ERR_BLOCKED_BY_CLIENT"


def test_cache_hits_are_flagged():
    t = feed(
        ResourceTracker(),
        sent("1", "https://x/logo.png", type_="Image"),
        received("1", type_="Image", mime="image/png", cached=True),
    )
    assert t.snapshot()[0].from_cache is True


def test_events_for_requests_started_before_attach_are_ignored():
    """没看到开头的请求就没有它的 URL 和方法，凭一条 loadingFinished 编不出记录。"""
    t = feed(ResourceTracker(), finished("ghost"), received("ghost", type_="Script"))
    assert t.snapshot() == []


def test_non_network_events_are_ignored():
    t = feed(
        ResourceTracker(),
        Event("Page.lifecycleEvent", {"name": "load"}),
        Event("Network.requestWillBeSent", {}),           # 没有 requestId
    )
    assert t.snapshot() == []


# --------------------------------------------------------------------------- #
# 分类的时机
# --------------------------------------------------------------------------- #


def test_type_arrives_with_the_response_and_backfills():
    """``requestWillBeSent`` 常常不带 type，只有响应里的才权威。"""
    t = feed(ResourceTracker(), sent("1", "https://x/a.css"))       # 没有 type
    assert t.snapshot()[0].resource_type == ""

    t.feed(received("1", type_="Stylesheet", mime="text/css"))
    assert t.snapshot()[0].resource_type == "Stylesheet"


def test_a_late_classification_still_triggers_the_callback():
    """回调的时机是"首次**满足筛选**"，不是"首次看见"。

    只在 requestWillBeSent 那一刻判断的实现会把这个资源整个漏掉 —— 那时它的 type
    还是空的。
    """
    seen: list[NetworkResource] = []
    t = ResourceTracker(types={"Stylesheet"}, on_discovered=seen.append)

    t.feed(sent("1", "https://x/a.css"))          # type 未知 → 还不该回调
    assert seen == []

    t.feed(received("1", type_="Stylesheet", mime="text/css"))
    assert [r.url for r in seen] == ["https://x/a.css"]


def test_the_callback_fires_once_per_resource_not_per_event():
    seen: list[str] = []
    t = ResourceTracker(types={"Script"}, on_discovered=lambda r: seen.append(r.url))
    feed(
        t,
        sent("1", "https://x/app.js", type_="Script"),
        received("1", type_="Script", mime="application/javascript"),
        finished("1"),
    )
    assert seen == ["https://x/app.js"]


def test_resource_type_from_a_failure_event_is_used_too():
    seen: list[str] = []
    t = ResourceTracker(types={"Script"}, on_discovered=lambda r: seen.append(r.url))
    feed(t, sent("1", "https://x/blocked.js"), failed("1", type_="Script"))
    assert seen == ["https://x/blocked.js"]


# --------------------------------------------------------------------------- #
# 重定向
# --------------------------------------------------------------------------- #


def test_a_redirect_chain_is_one_resource_ending_at_the_final_url():
    """CDP 的重定向复用同一个 requestId。不处理就会把中间跳当成最终结果。"""
    t = feed(
        ResourceTracker(),
        sent("1", "http://x/old.js", type_="Script"),
        redirected("1", "https://x/new.js"),
        received("1", type_="Script", mime="application/javascript"),
        finished("1"),
    )
    (r,) = t.snapshot()
    assert r.url == "https://x/new.js"
    assert r.redirects == ("http://x/old.js",)
    assert r.status == 200                     # 200，不是重定向那个 302


def test_multiple_hops_are_all_recorded():
    t = feed(
        ResourceTracker(),
        sent("1", "http://a/x", type_="Document"),
        redirected("1", "https://a/x"),
        redirected("1", "https://a/x/"),
    )
    (r,) = t.snapshot()
    assert r.redirects == ("http://a/x", "https://a/x")
    assert r.url == "https://a/x/"


# --------------------------------------------------------------------------- #
# 筛选与去重
# --------------------------------------------------------------------------- #


def test_types_filter_keeps_only_what_was_asked_for():
    t = feed(
        ResourceTracker(types={"Script", "Stylesheet"}),
        sent("1", "https://x/a.js", type_="Script"),
        sent("2", "https://x/a.css", type_="Stylesheet"),
        sent("3", "https://x/a.png", type_="Image"),
        sent("4", "https://x/api", type_="XHR"),
    )
    assert t.urls() == ["https://x/a.js", "https://x/a.css"]


def test_a_misspelled_resource_type_is_rejected_loudly():
    """静默抓不到东西比报错难查得多。"""
    with pytest.raises(ValueError, match="StyleSheet"):
        ResourceTracker(types={"Script", "StyleSheet"})
    assert "Stylesheet" in RESOURCE_TYPES


def test_predicate_composes_with_types():
    t = feed(
        ResourceTracker(types={"Script"}, predicate=lambda r: r.status == 200),
        sent("1", "https://x/ok.js", type_="Script"),
        received("1", type_="Script", status=200),
        sent("2", "https://x/gone.js", type_="Script"),
        received("2", type_="Script", status=404),
    )
    assert t.urls() == ["https://x/ok.js"]


def test_dedupe_by_url_collapses_repeat_requests():
    """同一个地址被请求两次（一次直连一次命中缓存）只报一次。"""
    seen: list[str] = []
    t = ResourceTracker(dedupe_by="url", on_discovered=lambda r: seen.append(r.url))
    feed(t, sent("1", "https://x/a.js", type_="Script"),
            sent("2", "https://x/a.js", type_="Script"))
    assert seen == ["https://x/a.js"]
    assert len(t.snapshot()) == 1


def test_dedupe_by_request_id_keeps_both():
    t = feed(
        ResourceTracker(dedupe_by="request_id"),
        sent("1", "https://x/a.js", type_="Script"),
        sent("2", "https://x/a.js", type_="Script"),
    )
    assert len(t.snapshot()) == 2


def test_dedupe_can_be_turned_off():
    t = feed(
        ResourceTracker(dedupe_by=None),
        sent("1", "https://x/a.js", type_="Script"),
        sent("2", "https://x/a.js", type_="Script"),
    )
    assert len(t.snapshot()) == 2


# --------------------------------------------------------------------------- #
# 取结果
# --------------------------------------------------------------------------- #


def test_snapshot_preserves_discovery_order():
    t = feed(
        ResourceTracker(),
        sent("1", "https://x/1", type_="Script"),
        sent("2", "https://x/2", type_="Script"),
        sent("3", "https://x/3", type_="Script"),
    )
    assert [r.url for r in t.snapshot()] == ["https://x/1", "https://x/2", "https://x/3"]


def test_snapshot_is_a_copy_not_a_live_view():
    t = feed(ResourceTracker(), sent("1", "https://x/1", type_="Script"))
    before = t.snapshot()
    t.feed(sent("2", "https://x/2", type_="Script"))
    assert len(before) == 1 and len(t.snapshot()) == 2


def test_urls_can_narrow_by_type():
    t = feed(
        ResourceTracker(),
        sent("1", "https://x/a.js", type_="Script"),
        sent("2", "https://x/a.css", type_="Stylesheet"),
    )
    assert t.urls("Script") == ["https://x/a.js"]
    assert t.urls("Stylesheet") == ["https://x/a.css"]
    assert len(t.urls()) == 2


def test_by_type_groups_and_len_counts():
    t = feed(
        ResourceTracker(),
        sent("1", "https://x/a.js", type_="Script"),
        sent("2", "https://x/b.js", type_="Script"),
        sent("3", "https://x/a.css", type_="Stylesheet"),
    )
    grouped = t.by_type()
    assert sorted(grouped) == ["Script", "Stylesheet"]
    assert len(grouped["Script"]) == 2
    assert len(t) == 3


def test_unclassified_resources_group_under_other():
    t = feed(ResourceTracker(), sent("1", "https://x/?", type_=None))
    assert list(t.by_type()) == ["Other"]


def test_reset_clears_everything_including_the_dedupe_memory():
    seen: list[str] = []
    t = ResourceTracker(on_discovered=lambda r: seen.append(r.url))
    t.feed(sent("1", "https://x/a.js", type_="Script"))
    t.reset()
    assert t.snapshot() == []

    t.feed(sent("1", "https://x/a.js", type_="Script"))
    assert seen == ["https://x/a.js"] * 2, "reset 之后同一个 URL 应该重新报一次"


def test_resource_str_is_readable():
    t = feed(
        ResourceTracker(),
        sent("1", "https://x/a.js", type_="Script"),
        received("1", type_="Script", status=200),
        finished("1"),
    )
    text = str(t.snapshot()[0])
    assert "Script" in text and "200" in text and "https://x/a.js" in text
