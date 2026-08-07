"""Provider 的状态码与生命周期语义。

打桩打在 ``urllib.request.urlopen`` 这一层，而不是 ``HttpClient.request``：401/403 →
:class:`AuthError` 的转换、``detail`` 解析、``HTTPError`` 到 :class:`ConnectionError`
的兜底全都住在 ``_http.py`` 里，替掉 ``request`` 就等于把它们一起绕过去了。

这一组测试真正要锁死的是**「不能用状态码元组表达生命周期语义」**：CloakBrowser 的
``stop`` 对「已停止」和「id 根本不存在」返回**完全相同**的 404 + 相同 detail
基类照状态码实现就会把打错的 id 静默当成幂等成功。
"""

from __future__ import annotations

import contextlib
import email.message
import io
import json
import urllib.error
import urllib.request
from datetime import timedelta

import pytest

from sleight.core.errors import AuthError, ConnectionError, InstanceError, NotFound, NotReady
from sleight.core.types import InstanceStatus
from sleight.providers.cloakbrowser import (
    CLEAR,
    SEED_MAX,
    TOKEN_ENV,
    UNSET,
    CloakBrowserManager,
    ProfileSpec,
)
from sleight.providers.plain import Plain

BASE = "http://mgr.test:19000"
TOKEN = "tok-abcdef0123456789"


# --------------------------------------------------------------------------- #
# 打桩
# --------------------------------------------------------------------------- #


class _Body:
    def __init__(self, status: int, payload: object) -> None:
        self.status = status
        self._raw = b"" if payload is None else json.dumps(payload).encode()
        self.headers = email.message.Message()
        self.headers.add_header("Content-Type", "application/json")

    def read(self) -> bytes:
        return self._raw

    def __enter__(self) -> _Body:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


class FakeManager:
    """按 ``(METHOD, path)`` 路由的假 HTTP 后端。

    每条路由是 ``(status, payload)``，或一个吃 ``(method, path, body)`` 的可调用对象
    （需要"第一次 stopped、第二次 running"这种时序时用）。
    """

    def __init__(self, routes: dict[tuple[str, str], object]) -> None:
        self.routes = routes
        self.log: list[tuple[str, str, object]] = []
        #: 每个请求用的超时。launch/stop 是同步等浏览器的，必须比通用超时长得多
        self.timeouts: list[tuple[str, str, float | None]] = []

    def install(self, monkeypatch: pytest.MonkeyPatch) -> FakeManager:
        monkeypatch.setattr(urllib.request, "urlopen", self._urlopen)
        return self

    def paths(self, method: str = "") -> list[str]:
        return [p for m, p, _ in self.log if not method or m == method]

    def bodies(self, method: str, path: str) -> list[object]:
        return [b for m, p, b in self.log if m == method and p == path]

    # ------------------------------------------------------------------ #

    def _urlopen(self, req, *, timeout=None, context=None):
        method = req.get_method()
        path = req.full_url[len(BASE):] if req.full_url.startswith(BASE) else req.full_url
        body = json.loads(req.data) if req.data else None
        self.log.append((method, path, body))
        self.timeouts.append((method, path, timeout))

        route = self.routes.get((method, path))
        if route is None:
            status, payload = 404, {"detail": f"no route for {method} {path}"}
        elif callable(route):
            status, payload = route(method, path, body)
        else:
            status, payload = route      # type: ignore[misc]

        if 200 <= status < 300:
            return _Body(status, payload)
        headers = email.message.Message()
        headers.add_header("Content-Type", "application/json")
        raw = b"" if payload is None else json.dumps(payload).encode()
        raise urllib.error.HTTPError(req.full_url, status, "error", headers, io.BytesIO(raw))


def manager(monkeypatch: pytest.MonkeyPatch, routes: dict) -> tuple[CloakBrowserManager, FakeManager]:
    http = FakeManager(routes).install(monkeypatch)
    mgr = CloakBrowserManager(BASE, token=TOKEN, name="cb")
    mgr.ready_poll = 0.001
    mgr.ready_timeout = 0.05
    return mgr, http


PROFILE_RUNNING = {"id": "p1", "name": "Win-US", "status": "running", "tags": []}
PROFILE_STOPPED = {"id": "p1", "name": "Win-US", "status": "stopped", "tags": []}
ST_RUNNING = (200, {"status": "running"})
ST_STOPPED = (200, {"status": "stopped"})
ST_MISSING = (404, {"detail": "Profile not found"})


# --------------------------------------------------------------------------- #
# 鉴权
# --------------------------------------------------------------------------- #


def test_a_token_is_mandatory(monkeypatch: pytest.MonkeyPatch):
    """WS 握手不带 Authorization 直接 403 —— 没 token 就别让它先跑起来。"""
    monkeypatch.delenv(TOKEN_ENV, raising=False)
    with pytest.raises(ValueError, match=TOKEN_ENV):
        CloakBrowserManager(BASE)


def test_the_token_can_come_from_the_environment(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(TOKEN_ENV, TOKEN)
    assert CloakBrowserManager(BASE).token == TOKEN


@pytest.mark.parametrize("status", [401, 403])
def test_auth_failures_are_not_retryable_errors(monkeypatch: pytest.MonkeyPatch, status: int):
    mgr, _ = manager(monkeypatch, {("GET", "/api/profiles"): (status, {"detail": "Unauthorized"})})
    with pytest.raises(AuthError) as exc:
        mgr.list_instances()
    assert str(status) in str(exc.value)


def test_the_token_never_appears_in_an_auth_error(monkeypatch: pytest.MonkeyPatch):
    """报错消息会进日志和 traceback。"""
    mgr, _ = manager(monkeypatch, {("GET", "/api/profiles"): (401, {"detail": "nope"})})
    with pytest.raises(AuthError) as exc:
        mgr.list_instances()
    assert TOKEN not in str(exc.value)


def test_the_bearer_header_rides_on_every_endpoint(monkeypatch: pytest.MonkeyPatch):
    mgr, _ = manager(monkeypatch, {})
    ep = mgr.endpoint("p1")
    assert ep.headers["Authorization"] == f"Bearer {TOKEN}"


# --------------------------------------------------------------------------- #
# status() 是唯一的存在性判据
# --------------------------------------------------------------------------- #


def test_status_distinguishes_missing_from_stopped(monkeypatch: pytest.MonkeyPatch):
    mgr, _ = manager(monkeypatch, {
        ("GET", "/api/profiles/p1/status"): ST_RUNNING,
        ("GET", "/api/profiles/p2/status"): ST_STOPPED,
        ("GET", "/api/profiles/zz/status"): ST_MISSING,
    })
    assert mgr.status("p1") is InstanceStatus.RUNNING
    assert mgr.status("p2") is InstanceStatus.STOPPED
    assert mgr.status("zz") is InstanceStatus.NOT_FOUND


def test_a_502_from_status_is_an_instance_error_not_a_verdict(monkeypatch: pytest.MonkeyPatch):
    """网关抽风不等于实例不存在 —— 不能悄悄当成 STOPPED 然后去 launch。"""
    mgr, _ = manager(monkeypatch, {("GET", "/api/profiles/p1/status"): (502, {"detail": "bad gw"})})
    with pytest.raises(InstanceError, match="502"):
        mgr.status("p1")


def test_an_unreachable_manager_is_a_connection_error(monkeypatch: pytest.MonkeyPatch):
    def boom(req, *, timeout=None, context=None):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    mgr = CloakBrowserManager(BASE, token=TOKEN)
    with pytest.raises(ConnectionError):
        mgr.list_instances()


# --------------------------------------------------------------------------- #
# ensure_ready / launch
# --------------------------------------------------------------------------- #


def test_ensure_ready_on_a_running_instance_sends_no_launch(monkeypatch: pytest.MonkeyPatch):
    """幂等 —— 已就绪就是 no-op，不该顺手重启别人正在用的浏览器。"""
    mgr, http = manager(monkeypatch, {("GET", "/api/profiles/p1/status"): ST_RUNNING})
    mgr.ensure_ready("p1")
    assert http.paths("POST") == []


def test_launch_409_means_already_running(monkeypatch: pytest.MonkeyPatch):
    """409 是"已在运行"，幂等成功。"""
    seen = {"n": 0}

    def status(*_):
        seen["n"] += 1
        return ST_STOPPED if seen["n"] == 1 else ST_RUNNING

    mgr, http = manager(monkeypatch, {
        ("GET", "/api/profiles/p1/status"): status,
        ("POST", "/api/profiles/p1/launch"): (409, {"detail": "Profile is already running"}),
    })
    mgr.ensure_ready("p1")                       # 不抛
    assert "/api/profiles/p1/launch" in http.paths("POST")


def test_launch_404_is_not_found_and_is_not_retried(monkeypatch: pytest.MonkeyPatch):
    """launch 的 404 语义是明确的（"Profile not found"），不同于 stop 的 404。"""
    mgr, _ = manager(monkeypatch, {
        ("GET", "/api/profiles/p1/status"): ST_STOPPED,       # 查完之后被人删了
        ("POST", "/api/profiles/p1/launch"): (404, {"detail": "Profile not found"}),
    })
    with pytest.raises(NotFound):
        mgr.ensure_ready("p1")


def test_ensure_ready_refuses_an_id_that_does_not_exist(monkeypatch: pytest.MonkeyPatch):
    """NotFound 只由 status() 给出，而且要在发 launch 之前就拦住。"""
    mgr, http = manager(monkeypatch, {("GET", "/api/profiles/zz/status"): ST_MISSING})
    with pytest.raises(NotFound):
        mgr.ensure_ready("zz")
    assert http.paths("POST") == []


def test_launch_5xx_is_an_instance_error(monkeypatch: pytest.MonkeyPatch):
    mgr, _ = manager(monkeypatch, {
        ("GET", "/api/profiles/p1/status"): ST_STOPPED,
        ("POST", "/api/profiles/p1/launch"): (502, {"detail": "upstream died"}),
    })
    with pytest.raises(InstanceError, match="502"):
        mgr.ensure_ready("p1")


def test_launch_that_never_becomes_ready_times_out(monkeypatch: pytest.MonkeyPatch):
    mgr, _ = manager(monkeypatch, {
        ("GET", "/api/profiles/p1/status"): ST_STOPPED,       # 永远起不来
        ("POST", "/api/profiles/p1/launch"): (200, {"status": "running"}),
    })
    with pytest.raises(InstanceError, match="not ready"):
        mgr.ensure_ready("p1")


# --------------------------------------------------------------------------- #
# stop 的 404 —— 这一节是整组测试的核心
# --------------------------------------------------------------------------- #


def test_stop_404_on_a_bogus_id_is_never_a_silent_success(monkeypatch: pytest.MonkeyPatch):
    """Manager 对「已停止」和「id 不存在」返回**同一个** 404 + 同一句 detail。

    按状态码判定的实现会把打错的 instance_id 当成幂等成功，而调用方以为自己停掉了
    某台机器。唯一的出路是回头问 ``status()``。
    """
    mgr, http = manager(monkeypatch, {
        ("POST", "/api/profiles/zz/stop"): (404, {"detail": "Profile is not running"}),
        ("GET", "/api/profiles/zz/status"): ST_MISSING,
    })
    with pytest.raises(NotFound):
        mgr.stop("zz")
    assert "/api/profiles/zz/status" in http.paths("GET"), "404 之后必须 status() 复核"


def test_stop_404_on_an_already_stopped_instance_is_idempotent(monkeypatch: pytest.MonkeyPatch):
    """同一个 404、同一句 detail，但 status() 说它存在 —— 这次才是幂等成功。"""
    mgr, _ = manager(monkeypatch, {
        ("POST", "/api/profiles/p1/stop"): (404, {"detail": "Profile is not running"}),
        ("GET", "/api/profiles/p1/status"): ST_STOPPED,
    })
    mgr.stop("p1")


def test_stop_5xx_is_an_instance_error(monkeypatch: pytest.MonkeyPatch):
    mgr, _ = manager(monkeypatch, {("POST", "/api/profiles/p1/stop"): (502, {"detail": "bad gw"})})
    with pytest.raises(InstanceError, match="502"):
        mgr.stop("p1")


# --------------------------------------------------------------------------- #
# recover
# --------------------------------------------------------------------------- #


def test_recover_stops_then_launches_then_waits(monkeypatch: pytest.MonkeyPatch):
    """只恢复连接，不重放业务操作。"""
    mgr, http = manager(monkeypatch, {
        ("GET", "/api/profiles/p1/status"): ST_RUNNING,
        ("POST", "/api/profiles/p1/stop"): (200, {"ok": True}),
        ("POST", "/api/profiles/p1/launch"): (200, {"status": "running"}),
    })
    mgr.recover("p1")
    assert http.paths("POST") == ["/api/profiles/p1/stop", "/api/profiles/p1/launch"]


def test_recover_tolerates_an_instance_that_is_already_down(monkeypatch: pytest.MonkeyPatch):
    mgr, http = manager(monkeypatch, {
        ("GET", "/api/profiles/p1/status"): ST_RUNNING,
        ("POST", "/api/profiles/p1/stop"): (404, {"detail": "Profile is not running"}),
        ("POST", "/api/profiles/p1/launch"): (200, {"status": "running"}),
    })
    mgr.recover("p1")
    assert "/api/profiles/p1/launch" in http.paths("POST")


def test_recover_refuses_an_unknown_id(monkeypatch: pytest.MonkeyPatch):
    mgr, http = manager(monkeypatch, {("GET", "/api/profiles/zz/status"): ST_MISSING})
    with pytest.raises(NotFound):
        mgr.recover("zz")
    assert http.paths("POST") == []


def test_release_does_not_stop_the_instance(monkeypatch: pytest.MonkeyPatch):
    """持久化登录态在浏览器里，停实例是运维动作，不是任务收尾动作。"""
    mgr, http = manager(monkeypatch, {})
    mgr.release("p1")
    assert http.log == []


# --------------------------------------------------------------------------- #
# 发现与 endpoint
# --------------------------------------------------------------------------- #


def test_tags_are_flattened_from_tag_colour_pairs(monkeypatch: pytest.MonkeyPatch):
    """Manager 返回 ``[{tag, color}]``；color 是展示层信息，路由只认 tag。"""
    mgr, _ = manager(monkeypatch, {("GET", "/api/profiles"): (200, [
        {"id": "p1", "name": "hk-1", "status": "running",
         "tags": [{"tag": "us", "color": "#f00"}, {"tag": "prod", "color": "#0f0"}]},
        {"id": "p2", "name": "hk-2", "status": "stopped", "tags": None},
    ])})
    a, b = mgr.list_instances()
    assert a.tags == frozenset({"us", "prod"})
    assert a.ready is True and b.ready is False
    assert a.uid == "cb:p1"
    assert b.tags == frozenset()


def test_a_non_list_profiles_response_is_an_error(monkeypatch: pytest.MonkeyPatch):
    mgr, _ = manager(monkeypatch, {("GET", "/api/profiles"): (200, {"oops": True})})
    with pytest.raises(InstanceError):
        mgr.list_instances()


def test_ws_url_is_built_from_our_base_not_the_managers(monkeypatch: pytest.MonkeyPatch):
    """``/json/version`` 的 ``webSocketDebuggerUrl`` 里 host 是**写死的** 127.0.0.1，
    换隧道端口或内网直连就错。"""
    mgr, _ = manager(monkeypatch, {})
    assert mgr.endpoint("p1").ws_url == "ws://mgr.test:19000/api/profiles/p1/cdp"


def test_https_base_yields_a_wss_url(monkeypatch: pytest.MonkeyPatch):
    FakeManager({}).install(monkeypatch)
    mgr = CloakBrowserManager("https://mgr.test", token=TOKEN)
    assert mgr.endpoint("p1").ws_url.startswith("wss://mgr.test/")


def test_endpoint_without_an_instance_id_is_rejected(monkeypatch: pytest.MonkeyPatch):
    mgr, _ = manager(monkeypatch, {})
    with pytest.raises(ValueError):
        mgr.endpoint()


# --------------------------------------------------------------------------- #
# Profile 组装
# --------------------------------------------------------------------------- #


def test_ensure_profile_creates_when_the_name_is_new(monkeypatch: pytest.MonkeyPatch):
    mgr, http = manager(monkeypatch, {
        ("GET", "/api/profiles"): (200, []),
        ("POST", "/api/profiles"): (200, PROFILE_STOPPED),
    })
    info = mgr.ensure_profile(ProfileSpec.windows_us("Win-US"))
    assert info.id == "p1"
    assert http.bodies("POST", "/api/profiles")[0]["name"] == "Win-US"    # type: ignore[index]


def test_ensure_profile_is_a_noop_when_nothing_differs(monkeypatch: pytest.MonkeyPatch):
    spec = ProfileSpec.windows_us("Win-US")
    existing = {**PROFILE_STOPPED, **spec.to_payload(), "tags": []}
    mgr, http = manager(monkeypatch, {("GET", "/api/profiles"): (200, [existing])})
    mgr.ensure_profile(spec)
    assert http.paths("PUT") == []


def test_ensure_profile_pushes_a_retag(monkeypatch: pytest.MonkeyPatch):
    """tags 是 Pool 的路由键（``where=lambda i: "us" in i.tags``）。

    服务端存 ``[{tag, color}]``、spec 给 ``[{tag}]``，直接比字典永远不等 —— 把 tags
    排除在 diff 之外就会导致**改了 tag 重跑完全不发 PUT**，重新打标的 profile 继续
    被旧谓词选中。
    """
    spec = ProfileSpec.windows_us("Win-US", tags=("sg",))
    existing = {**PROFILE_STOPPED, **spec.to_payload(),
                "tags": [{"tag": "us", "color": "#f00"}]}
    mgr, http = manager(monkeypatch, {
        ("GET", "/api/profiles"): (200, [existing]),
        ("PUT", "/api/profiles/p1"): (200, {**PROFILE_STOPPED, "tags": [{"tag": "sg"}]}),
    })
    info = mgr.ensure_profile(spec)
    assert http.bodies("PUT", "/api/profiles/p1")[0]["tags"] == [{"tag": "sg"}]  # type: ignore[index]
    assert info.tags == frozenset({"sg"})


def test_ensure_profile_pushes_a_changed_proxy(monkeypatch: pytest.MonkeyPatch):
    spec = ProfileSpec.windows_us("Win-US", proxy="socks5://u:p@hk.example:3000")
    existing = {**PROFILE_STOPPED, **spec.to_payload(), "proxy": "socks5://old:1080", "tags": []}
    mgr, http = manager(monkeypatch, {
        ("GET", "/api/profiles"): (200, [existing]),
        ("PUT", "/api/profiles/p1"): (200, PROFILE_STOPPED),
    })
    mgr.ensure_profile(spec)
    assert http.bodies("PUT", "/api/profiles/p1")[0]["proxy"] == spec.proxy  # type: ignore[index]


def test_create_profile_can_launch_right_away(monkeypatch: pytest.MonkeyPatch):
    mgr, http = manager(monkeypatch, {
        ("POST", "/api/profiles"): (200, PROFILE_STOPPED),
        ("GET", "/api/profiles/p1/status"): ST_RUNNING,
    })
    info = mgr.create_profile(ProfileSpec.windows_us("Win-US"), launch=True)
    assert info.ready is True
    assert "/api/profiles/p1/status" in http.paths("GET")


def test_create_profile_rejects_a_self_contradictory_spec(monkeypatch: pytest.MonkeyPatch):
    mgr, http = manager(monkeypatch, {("POST", "/api/profiles"): (200, PROFILE_STOPPED)})
    bad = ProfileSpec(name="Bad", platform="windows", gpu_renderer="ANGLE (Apple, Metal)")
    with pytest.raises(ValueError):
        mgr.create_profile(bad)
    assert http.log == [], "自相矛盾的指纹不该先发出去再说"


def test_get_profile_404_is_not_found(monkeypatch: pytest.MonkeyPatch):
    mgr, _ = manager(monkeypatch, {("GET", "/api/profiles/zz"): ST_MISSING})
    with pytest.raises(NotFound):
        mgr.get_profile("zz")


def test_update_profile_404_is_not_found(monkeypatch: pytest.MonkeyPatch):
    mgr, _ = manager(monkeypatch, {("PUT", "/api/profiles/zz"): ST_MISSING})
    with pytest.raises(NotFound):
        mgr.update_profile("zz", notes="x")


def test_delete_refuses_a_running_profile_without_force(monkeypatch: pytest.MonkeyPatch):
    """删 profile 会连带删掉 ``user_data_dir`` 里的登录态，不可逆。"""
    mgr, http = manager(monkeypatch, {("GET", "/api/profiles/p1/status"): ST_RUNNING})
    with pytest.raises(InstanceError, match="force=True"):
        mgr.delete_profile("p1")
    assert http.paths("DELETE") == []


def test_delete_with_force_stops_first(monkeypatch: pytest.MonkeyPatch):
    mgr, http = manager(monkeypatch, {
        ("GET", "/api/profiles/p1/status"): ST_RUNNING,
        ("POST", "/api/profiles/p1/stop"): (200, {"ok": True}),
        ("DELETE", "/api/profiles/p1"): (200, {"ok": True}),
    })
    mgr.delete_profile("p1", force=True)
    assert http.paths("POST") == ["/api/profiles/p1/stop"]
    assert http.paths("DELETE") == ["/api/profiles/p1"]


def test_delete_of_a_stopped_profile_needs_no_force(monkeypatch: pytest.MonkeyPatch):
    mgr, http = manager(monkeypatch, {
        ("GET", "/api/profiles/p1/status"): ST_STOPPED,
        ("DELETE", "/api/profiles/p1"): (200, {"ok": True}),
    })
    mgr.delete_profile("p1")
    assert http.paths("POST") == []


def test_delete_of_an_unknown_id_is_not_found(monkeypatch: pytest.MonkeyPatch):
    mgr, _ = manager(monkeypatch, {("GET", "/api/profiles/zz/status"): ST_MISSING})
    with pytest.raises(NotFound):
        mgr.delete_profile("zz")


def test_system_status_passes_through(monkeypatch: pytest.MonkeyPatch):
    payload = {"running_count": 1, "binary_version": "146.0.7680.177.5", "profiles_total": 1}
    mgr, _ = manager(monkeypatch, {("GET", "/api/status"): (200, payload)})
    assert mgr.system_status() == payload


# --------------------------------------------------------------------------- #
# Plain
# --------------------------------------------------------------------------- #


class FakePlain(FakeManager):
    """Plain 连的是浏览器自己的 ``/json/version``，base 不一样。"""

    def _urlopen(self, req, *, timeout=None, context=None):
        req.full_url = req.full_url.replace("http://127.0.0.1:9222", BASE)
        return super()._urlopen(req, timeout=timeout, context=context)


def plain(monkeypatch: pytest.MonkeyPatch, routes: dict) -> Plain:
    FakePlain(routes).install(monkeypatch)
    return Plain("http://127.0.0.1:9222")


def test_plain_takes_the_ws_url_from_json_version(monkeypatch: pytest.MonkeyPatch):
    p = plain(monkeypatch, {("GET", "/json/version"): (
        200, {"webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/browser/abc"})})
    assert p.endpoint().ws_url.endswith("/devtools/browser/abc")


def test_plain_reports_stopped_when_nothing_answers(monkeypatch: pytest.MonkeyPatch):
    def refused(req, *, timeout=None, context=None):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", refused)
    p = Plain("http://127.0.0.1:9222")
    assert p.status("default") is InstanceStatus.STOPPED


def test_plain_cannot_start_a_browser(monkeypatch: pytest.MonkeyPatch):
    """没有管理 API —— 说不行比假装成功强。"""
    def refused(req, *, timeout=None, context=None):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", refused)
    with pytest.raises(NotReady, match="launch it yourself"):
        Plain("http://127.0.0.1:9222").ensure_ready("default")


def test_plain_has_exactly_one_instance(monkeypatch: pytest.MonkeyPatch):
    p = plain(monkeypatch, {("GET", "/json/version"): (200, {"webSocketDebuggerUrl": "ws://x"})})
    assert [i.id for i in p.list_instances()] == ["default"]
    assert p.status("other") is InstanceStatus.NOT_FOUND
    with pytest.raises(NotFound):
        p.endpoint("other")


def test_plain_without_a_ws_url_is_a_connection_error(monkeypatch: pytest.MonkeyPatch):
    p = plain(monkeypatch, {("GET", "/json/version"): (200, {"Browser": "Chrome/146"})})
    with pytest.raises(ConnectionError, match="webSocketDebuggerUrl"):
        p.endpoint()


# --------------------------------------------------------------------------- #
# 原始字段与 CDP target 列表（运维层要用的两个读接口）
# --------------------------------------------------------------------------- #


def test_list_profiles_keeps_the_fields_instanceinfo_drops(monkeypatch: pytest.MonkeyPatch):
    """InstanceInfo 只留驱动层要的四个字段；运维要看 launch_args / proxy / notes。"""
    raw = {**PROFILE_RUNNING, "launch_args": ["--lang=en-US"], "proxy": "socks5://h:1"}
    mgr, _ = manager(monkeypatch, {("GET", "/api/profiles"): (200, [raw])})
    assert mgr.list_profiles() == [raw]


def test_list_profiles_rejects_a_non_list(monkeypatch: pytest.MonkeyPatch):
    mgr, _ = manager(monkeypatch, {("GET", "/api/profiles"): (200, {"detail": "nope"})})
    with pytest.raises(InstanceError):
        mgr.list_profiles()


def test_cdp_targets_returns_the_target_list(monkeypatch: pytest.MonkeyPatch):
    targets = [{"type": "page", "url": "https://example.com"}]
    mgr, _ = manager(monkeypatch, {("GET", "/api/profiles/p1/cdp/json/list"): (200, targets)})
    assert mgr.cdp_targets("p1") == targets


def test_cdp_targets_of_a_stopped_instance_raises_not_ready(monkeypatch: pytest.MonkeyPatch):
    """「实例没起来」和「跑着但扩展没加载」都返回 [] 的话，前者会被读成后者。

    而这两件事的处理方式完全相反：一个要 launch，一个要查扩展。
    """
    mgr, _ = manager(
        monkeypatch,
        {("GET", "/api/profiles/p1/cdp/json/list"): (404, {"detail": "Profile not running"})},
    )
    with pytest.raises(NotReady, match="not running"):
        mgr.cdp_targets("p1")


def test_a_running_instance_with_no_targets_is_still_an_empty_list(
    monkeypatch: pytest.MonkeyPatch,
):
    mgr, _ = manager(monkeypatch, {("GET", "/api/profiles/p1/cdp/json/list"): (200, [])})
    assert mgr.cdp_targets("p1") == []


def test_cdp_targets_5xx_is_an_instance_error(monkeypatch: pytest.MonkeyPatch):
    mgr, _ = manager(
        monkeypatch, {("GET", "/api/profiles/p1/cdp/json/list"): (502, {"detail": "bad gateway"})}
    )
    with pytest.raises(InstanceError):
        mgr.cdp_targets("p1")


# --------------------------------------------------------------------------- #
# 生命周期接口的超时
# --------------------------------------------------------------------------- #


def test_launch_gets_a_long_timeout_because_it_waits_for_a_browser(monkeypatch: pytest.MonkeyPatch):
    """``POST /launch`` 是同步的 —— 它等 Chromium 真的起来才返回。

    实测冷启动约 69 秒（3.8 GB 内存的机器）。用通用的 15s 超时的话，会在浏览器
    **其实已经起来**的情况下抛 ConnectionError，而调用方从那个异常里根本看不出
    实例到底起没起。
    """
    mgr, http = manager(monkeypatch, {
        ("GET", "/api/profiles/p1/status"): ST_STOPPED,
        ("POST", "/api/profiles/p1/launch"): (200, {"status": "running"}),
    })
    mgr.ready_timeout = 0.0                      # 只看 launch 那一发
    with contextlib.suppress(InstanceError):
        mgr.ensure_ready("p1")

    launch = [t for m, p, t in http.timeouts if m == "POST" and p.endswith("/launch")]
    status = [t for m, p, t in http.timeouts if m == "GET" and p.endswith("/status")]
    assert launch == [mgr.lifecycle_timeout]
    assert mgr.lifecycle_timeout >= 120.0, "冷启动一个 profile 实测就要 69 秒"
    assert status[0] != mgr.lifecycle_timeout, "普通读接口不该也用这个长超时"


def test_stop_gets_the_same_long_timeout(monkeypatch: pytest.MonkeyPatch):
    """停一个 profile 也要等浏览器进程真的退出。"""
    mgr, http = manager(monkeypatch, {("POST", "/api/profiles/p1/stop"): (200, {"ok": True})})
    mgr.stop("p1")
    assert [t for m, p, t in http.timeouts if m == "POST"] == [mgr.lifecycle_timeout]


# --------------------------------------------------------------------------- #
# UNSET / CLEAR —— 消灭 None vs "" 的陷阱
# --------------------------------------------------------------------------- #


def test_unset_and_none_are_both_left_out_of_the_payload():
    spec = ProfileSpec(name="x", proxy=UNSET, notes=None)
    payload = spec.to_payload()
    assert "proxy" not in payload and "notes" not in payload


def test_clear_is_sent_as_an_empty_value():
    assert ProfileSpec(name="x", proxy=CLEAR).to_payload()["proxy"] == ""


def test_a_bare_empty_string_is_refused_by_validate():
    """proxy='' 清空、proxy=None 保留 —— 只差一个字符，做的是相反的事。"""
    with pytest.raises(ValueError, match="CLEAR"):
        ProfileSpec(name="x", proxy="").validate()


def test_clear_does_not_confuse_the_geoip_check():
    """CLEAR 代表"要清掉"，所以 geoip 仍然是"没有代理可推导"。"""
    with pytest.raises(ValueError, match="nothing to derive"):
        ProfileSpec(name="x", geoip=True, proxy=CLEAR).validate()


def test_clear_does_not_break_the_gpu_string_check():
    ProfileSpec(name="x", platform="macos", gpu_renderer=CLEAR, gpu_vendor=CLEAR).validate()


def test_update_profile_sends_an_empty_string_for_clear(monkeypatch: pytest.MonkeyPatch):
    mgr, http = manager(monkeypatch, {("PUT", "/api/profiles/p1"): (200, PROFILE_RUNNING)})
    mgr.update_profile("p1", proxy=CLEAR)
    assert http.bodies("PUT", "/api/profiles/p1") == [{"proxy": ""}]


def test_update_profile_drops_unset_fields(monkeypatch: pytest.MonkeyPatch):
    mgr, http = manager(monkeypatch, {("PUT", "/api/profiles/p1"): (200, PROFILE_RUNNING)})
    mgr.update_profile("p1", proxy=UNSET, notes="kept")
    assert http.bodies("PUT", "/api/profiles/p1") == [{"notes": "kept"}]


def test_update_profile_refuses_a_bare_empty_string(monkeypatch: pytest.MonkeyPatch):
    mgr, http = manager(monkeypatch, {("PUT", "/api/profiles/p1"): (200, PROFILE_RUNNING)})
    with pytest.raises(ValueError, match="CLEAR"):
        mgr.update_profile("p1", proxy="")
    assert not http.paths("PUT"), "sent the request anyway"


def test_an_update_that_changes_nothing_is_a_mistake(monkeypatch: pytest.MonkeyPatch):
    mgr, http = manager(monkeypatch, {("PUT", "/api/profiles/p1"): (200, PROFILE_RUNNING)})
    with pytest.raises(ValueError, match="nothing to change"):
        mgr.update_profile("p1", proxy=UNSET)
    assert not http.paths("PUT")


# --------------------------------------------------------------------------- #
# #8 prune_profiles / #9 restart / #11 随机指纹种子
# --------------------------------------------------------------------------- #


def profile(name, *, pid=None, tags=(), created=None, status="stopped"):
    raw = {"id": pid or name, "name": name, "status": status,
           "tags": [{"tag": t} for t in tags]}
    if created:
        raw["created_at"] = created
    return raw


FLEET = [
    profile("reuters-test-1", tags=("scratch",), created="2026-01-01T00:00:00Z"),
    profile("reuters-test-2", tags=("scratch", "us"), created="2026-08-06T00:00:00Z"),
    profile("prod-hk-01", tags=("us",), created="2026-01-01T00:00:00Z"),
    profile("no-timestamp"),
]


def fleet_manager(monkeypatch, extra=None):
    routes = {("GET", "/api/profiles"): (200, FLEET), **(extra or {})}
    for p in FLEET:
        routes.setdefault(("GET", f"/api/profiles/{p['id']}/status"), ST_STOPPED)
        routes.setdefault(("DELETE", f"/api/profiles/{p['id']}"), (200, {}))
    return manager(monkeypatch, routes)


def test_prune_is_dry_by_default(monkeypatch: pytest.MonkeyPatch):
    """删 profile 连带删 user_data_dir，不可逆 —— 默认必须只是预演。"""
    mgr, http = fleet_manager(monkeypatch)
    doomed = mgr.prune_profiles(name_prefix="reuters-test-")
    assert [p["name"] for p in doomed] == ["reuters-test-1", "reuters-test-2"]
    assert not http.paths("DELETE"), "dry_run deleted something"


def test_prune_actually_deletes_when_told_to(monkeypatch: pytest.MonkeyPatch):
    mgr, http = fleet_manager(monkeypatch)
    gone = mgr.prune_profiles(name_prefix="reuters-test-", dry_run=False)
    assert [p["name"] for p in gone] == ["reuters-test-1", "reuters-test-2"]
    assert sorted(http.paths("DELETE")) == [
        "/api/profiles/reuters-test-1", "/api/profiles/reuters-test-2",
    ]


def test_prune_with_no_filter_is_refused(monkeypatch: pytest.MonkeyPatch):
    """"删掉全部"必须是写出来的意图，不能是少传一个参数的后果。"""
    mgr, http = fleet_manager(monkeypatch)
    with pytest.raises(ValueError, match='name_prefix=""'):
        mgr.prune_profiles(dry_run=False)
    assert not http.paths("DELETE")


def test_prune_by_tag(monkeypatch: pytest.MonkeyPatch):
    mgr, _ = fleet_manager(monkeypatch)
    assert {p["name"] for p in mgr.prune_profiles(tags=["scratch"])} == {
        "reuters-test-1", "reuters-test-2"
    }


def test_prune_filters_are_and_not_or(monkeypatch: pytest.MonkeyPatch):
    mgr, _ = fleet_manager(monkeypatch)
    assert [p["name"] for p in mgr.prune_profiles(name_prefix="prod-", tags=["us"])] == [
        "prod-hk-01"
    ]


def test_prune_older_than_skips_profiles_with_no_timestamp(monkeypatch: pytest.MonkeyPatch):
    """读不出建立时间就不删 —— 批量删除里"看不懂就不动"是唯一安全的默认值。"""
    mgr, _ = fleet_manager(monkeypatch)
    names = {p["name"] for p in mgr.prune_profiles(
        name_prefix="", older_than=timedelta(days=30)
    )}
    assert "no-timestamp" not in names
    assert "reuters-test-1" in names and "prod-hk-01" in names
    assert "reuters-test-2" not in names, "that one is recent"


def test_one_undeletable_profile_does_not_strand_the_rest(monkeypatch: pytest.MonkeyPatch):
    """删了一半就报错是批量清理最难收拾的状态。"""
    mgr, _http = fleet_manager(
        monkeypatch,
        {("DELETE", "/api/profiles/reuters-test-1"): (500, {"detail": "busy"})},
    )
    gone = mgr.prune_profiles(name_prefix="reuters-test-", dry_run=False)
    assert [p["name"] for p in gone] == ["reuters-test-2"]


def test_update_profile_restart_restarts_a_running_instance(monkeypatch: pytest.MonkeyPatch):
    """proxy / fingerprint_seed 改了不重启就不生效，而 Manager 只提示不执行。"""
    mgr, http = manager(monkeypatch, {
        ("PUT", "/api/profiles/p1"): (200, PROFILE_RUNNING),
        ("GET", "/api/profiles/p1/status"): ST_RUNNING,
        ("POST", "/api/profiles/p1/stop"): (200, {}),
        ("POST", "/api/profiles/p1/launch"): (200, {}),
        ("GET", "/api/profiles/p1"): (200, PROFILE_RUNNING),
    })
    mgr.update_profile("p1", proxy="socks5://new:1080", restart=True)
    assert "/api/profiles/p1/stop" in http.paths("POST")
    assert "/api/profiles/p1/launch" in http.paths("POST")


def test_update_profile_restart_does_not_launch_a_stopped_instance(
    monkeypatch: pytest.MonkeyPatch,
):
    """拉起一个本来没在跑的实例是另一个决定，而且要占一份内存。"""
    mgr, http = manager(monkeypatch, {
        ("PUT", "/api/profiles/p1"): (200, PROFILE_STOPPED),
        ("GET", "/api/profiles/p1/status"): ST_STOPPED,
        ("GET", "/api/profiles/p1"): (200, PROFILE_STOPPED),
    })
    mgr.update_profile("p1", proxy="socks5://new:1080", restart=True)
    assert not http.paths("POST")


def test_update_profile_does_not_restart_by_default(monkeypatch: pytest.MonkeyPatch):
    mgr, http = manager(monkeypatch, {("PUT", "/api/profiles/p1"): (200, PROFILE_RUNNING)})
    mgr.update_profile("p1", proxy="socks5://new:1080")
    assert not http.paths("POST")


def test_a_random_seed_is_drawn_at_payload_time():
    a = ProfileSpec(name="x", fingerprint_seed="random").to_payload()["fingerprint_seed"]
    b = ProfileSpec(name="x", fingerprint_seed="random").to_payload()["fingerprint_seed"]
    assert isinstance(a, int) and 1 <= a <= SEED_MAX
    assert a != b, "every profile getting the same seed is the bug this feature fixes"


def test_randomized_picks_a_preset_and_a_seed():
    spec = ProfileSpec.randomized("scrape-01", "windows_hk")
    assert spec.fingerprint_seed == "random"
    assert spec.timezone == "Asia/Hong_Kong"
    assert isinstance(spec.to_payload()["fingerprint_seed"], int)


def test_randomized_rejects_an_unknown_preset():
    with pytest.raises(ValueError, match="unknown preset"):
        ProfileSpec.randomized("x", "windows_jp")


def test_a_misspelled_random_is_caught_not_sent():
    with pytest.raises(ValueError, match="fingerprint_seed"):
        ProfileSpec(name="x", fingerprint_seed="ramdom").validate()


def test_an_out_of_range_seed_is_caught():
    with pytest.raises(ValueError, match="outside"):
        ProfileSpec(name="x", fingerprint_seed=0).validate()


def test_ensure_profile_does_not_reroll_an_existing_fingerprint(
    monkeypatch: pytest.MonkeyPatch,
):
    """"random" 是"建的时候摇一个"，不是"每次 ensure 都换一张身份证"。

    不摘掉的话 to_payload() 每次给新值 → diff 必然非空 → 每跑一遍脚本指纹就变一次。
    """
    existing = {"id": "p1", "name": "Win-US", "status": "stopped", "tags": [],
                "fingerprint_seed": 4242}
    mgr, http = manager(monkeypatch, {
        ("GET", "/api/profiles"): (200, [existing]),
        ("PUT", "/api/profiles/p1"): (200, existing),
    })
    mgr.ensure_profile(ProfileSpec.randomized("Win-US"))
    bodies = http.bodies("PUT", "/api/profiles/p1")
    assert all("fingerprint_seed" not in (b or {}) for b in bodies), (
        f"rerolled the fingerprint: {bodies}"
    )
