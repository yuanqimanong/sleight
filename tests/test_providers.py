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

import pytest

from sleight.core.errors import AuthError, ConnectionError, InstanceError, NotFound, NotReady
from sleight.core.types import InstanceStatus
from sleight.providers.cloakbrowser import TOKEN_ENV, CloakBrowserManager, ProfileSpec
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


def test_cdp_targets_of_a_stopped_instance_is_empty_not_an_error(monkeypatch: pytest.MonkeyPatch):
    """停止的实例访问 CDP 返回 404 —— 那是"没有 target"，不是故障。"""
    mgr, _ = manager(
        monkeypatch,
        {("GET", "/api/profiles/p1/cdp/json/list"): (404, {"detail": "Profile not running"})},
    )
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
