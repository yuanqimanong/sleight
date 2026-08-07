"""BrowserContext：隔离的 cookie jar、storage，以及独立的 socket pool。

最后那条才是它在 antidetect 场景里的价值 —— 也是最容易在重构里被无声破坏的那条：
只要 tab 不是建在 context 里，一切照样"能跑"，只是出口再也不换了。
"""

from __future__ import annotations

import pytest

from sleight import Pool
from sleight.core.errors import ProtocolError, SleightError
from sleight.pool import BrowserContext

from .conftest import FakeProvider


class FakeBrowser:
    """浏览器级 WS 的替身。只认 Target domain，其余一律回空。"""

    def __init__(self, *, fail: set[str] | None = None) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.contexts: set[str] = set()
        self.targets: dict[str, str | None] = {}       # target id → context id
        self.closed = False
        self._fail = fail or set()
        self._n = 0

    # —— Transport 接口 ——

    def call(self, method, params=None, *, session_id=None, **kw):
        params = params or {}
        if self.closed:
            # 真 Transport 也是这个行为。断言"顺序对不对"全靠它
            raise SleightError(f"{method} on a closed transport")
        self.calls.append((method, params))
        if method in self._fail:
            raise ProtocolError(f"{method} refused by the fake browser")
        self._n += 1
        match method:
            case "Target.createBrowserContext":
                cid = f"CTX{self._n}"
                self.contexts.add(cid)
                return {"browserContextId": cid}
            case "Target.disposeBrowserContext":
                self.contexts.discard(params["browserContextId"])
            case "Target.createTarget":
                tid = f"T{self._n}"
                self.targets[tid] = params.get("browserContextId")
                return {"targetId": tid}
            case "Target.closeTarget":
                self.targets.pop(params["targetId"], None)
            case "Target.attachToTarget":
                return {"sessionId": f"S{self._n}"}
        return {}

    def send_no_wait(self, method, params=None, *, session_id=None):
        return 0

    def flush(self, **kw) -> None:
        pass

    def drain_events(self, session_id=None):
        return iter(())

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def browser(monkeypatch) -> FakeBrowser:
    fake = FakeBrowser()
    monkeypatch.setattr("sleight.pool.Transport.connect", lambda *a, **kw: fake)
    # Session 构造时会 enable 一串 domain 并 drain，走的都是 FakeBrowser.call
    return fake


@pytest.fixture
def handle(browser: FakeBrowser):
    with Pool([FakeProvider(1)]).lease() as h:
        yield h


def created(browser: FakeBrowser, method: str) -> list[dict]:
    return [p for m, p in browser.calls if m == method]


# --------------------------------------------------------------------------- #


def test_a_tab_opened_in_a_context_really_carries_the_context_id(handle, browser):
    """这是整个特性唯一的实质动作。传丢了的话一切照跑，只是隔离没了。"""
    with handle.context() as ctx:
        ctx.session()
        assert browser.targets == {"T2": ctx.context_id}


def test_a_plain_session_stays_in_the_default_context(handle, browser):
    handle.session()
    assert list(browser.targets.values()) == [None]
    assert "browserContextId" not in created(browser, "Target.createTarget")[0]


def test_omitting_proxy_inherits_the_process_level_one(handle, browser):
    """不传 proxyServer 就继承 --proxy-server —— 而且这样已经足够换出口。"""
    handle.context()
    params = created(browser, "Target.createBrowserContext")[0]
    assert "proxyServer" not in params
    assert params["disposeOnDetach"] is True


def test_proxy_and_bypass_are_passed_through(handle, browser):
    handle.context(proxy="socks5://127.0.0.1:1080", proxy_bypass="localhost")
    params = created(browser, "Target.createBrowserContext")[0]
    assert params["proxyServer"] == "socks5://127.0.0.1:1080"
    assert params["proxyBypassList"] == "localhost"


def test_leaving_the_with_block_closes_the_tab_and_disposes_the_context(handle, browser):
    with handle.context() as ctx:
        ctx.session()
        cid = ctx.context_id
    assert browser.targets == {}, "orphaned target left in the browser"
    assert browser.contexts == set(), f"orphaned context {cid}"


def test_a_failing_target_close_still_disposes_the_context(monkeypatch, handle, browser):
    """三步各自独立 try。第一步炸了就跳过后面的话，孤儿 context 会一直攒。"""
    def boom() -> None:
        raise SleightError("tab is gone")

    ctx = handle.context()
    monkeypatch.setattr(ctx.session(), "close", boom)

    with pytest.raises(ExceptionGroup):
        ctx.close()
    assert browser.contexts == set(), "gave up on the context because a tab failed to close"
    monkeypatch.undo()                       # 别把这个坏 close 留给 handle 的拆卸


def test_close_is_idempotent(handle, browser):
    ctx = handle.context()
    ctx.close()
    ctx.close()
    assert len(created(browser, "Target.disposeBrowserContext")) == 1


def test_a_disposed_context_refuses_to_hand_out_more_sessions(handle):
    ctx = handle.context()
    ctx.close()
    with pytest.raises(SleightError, match="disposed"):
        ctx.session()


def test_forgetting_to_close_the_context_does_not_leak_it(browser):
    """handle 兜底。忘了关 context 是最容易犯的错，而攒出来的东西完全看不见。"""
    with Pool([FakeProvider(1)]).lease() as h:
        ctx = h.context()
        ctx.session()
    assert browser.contexts == set()
    assert browser.targets == {}


def test_contexts_are_disposed_before_the_socket_goes_away(browser):
    """顺序是硬的：transport 关掉之后，这条命令根本发不出去了。

    ``FakeBrowser`` 在关闭后对任何 ``call`` 都抛 —— 顺序反了这里就是 ExceptionGroup。
    """
    with Pool([FakeProvider(1)]).lease() as h:
        h.context()
    assert browser.closed
    assert "Target.disposeBrowserContext" in [m for m, _ in browser.calls]


def test_a_context_that_cannot_be_created_does_not_register_anything(handle):
    broken = FakeBrowser(fail={"Target.createBrowserContext"})
    handle._transport = broken
    with pytest.raises(ProtocolError):
        handle.context()
    assert handle._contexts == []


def test_repr_says_whether_it_is_still_alive(handle):
    ctx = handle.context()
    assert "closed" not in repr(ctx)
    ctx.close()
    assert "closed" in repr(ctx)
    assert isinstance(ctx, BrowserContext)
