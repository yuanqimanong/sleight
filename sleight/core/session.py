"""Session：一个 tab 上的操作会话。

**默认创建自有 target**，退出时关闭。接管既有 tab 必须显式 :meth:`Session.attach`。

不去挑 ``Target.getTargets()`` 里第一个 ``type=="page"``：target 顺序没有业务语义，
可能选中用户正在用的页面、扩展页、或上次任务的遗留页；叠加"只关自己创建的 tab"这条
规则，结果就是默认会修改一个别人的页面、退出时既不恢复也不关闭。

「使用既有持久化 Context」≠「接管既有 tab」—— ``Target.createTarget`` 不带
``browserContextId`` 就落在默认 context 里，Cookie 和登录态照样继承。
"""

from __future__ import annotations

import base64
import contextlib
import json
import logging
import time
from collections.abc import Callable, Iterable, Iterator
from random import Random
from typing import Any

from .element import Element
from .errors import ElementError, ProtocolError, SleightError, TimeoutError
from .human.presets import HumanProfile
from .input import HumanSwitch, InputDriver
from .netidle import NetworkIdleTracker
from .protocol import Event
from .resources import DedupeKey, NetworkResource, ResourceTracker
from .transport import Transport
from .types import Box, Condition, DomReady, Point

log = logging.getLogger("sleight.session")

__all__ = ["NetworkResource", "Selectable", "Session"]

_POLL_MIN = 0.10
_POLL_MAX = 0.25
_PUMP_SLICE = 0.05

#: 无字段的 frozen dataclass，共享一个实例即可（也让 ruff B008 满意）
_DEFAULT_WAIT = DomReady()

#: 交互方法收的目标形态。叫 ``Selectable`` 而不是 ``Target`` —— 这个文件里
#: ``Target.createTarget`` / ``Target.attachToTarget`` 满地都是，同名会读岔
Selectable = str | Element


class Session:
    """一个 tab 上的操作会话。

    **不是线程安全的，也不需要是** —— 一个实例同时只给一个 Session，每个线程
    ``lease()`` 自己的实例。

    一般不直接构造，用 :meth:`create` / :meth:`attach`，或者
    :meth:`InstanceHandle.session() <sleight.pool.InstanceHandle.session>`。

    :param transport: 已连上的浏览器级 WebSocket
    :param session_id: ``Target.attachToTarget`` 返回的 CDP sessionId
    :param target_id: 这个会话操作的 target
    :param owned_target: True 表示 target 是自己建的，:meth:`close` 时要关掉它；
        False 表示接管的，只 detach
    :param track_network: 是否 ``Network.enable``。关掉就用不了
        :class:`~sleight.core.types.NetworkIdle` 和 :meth:`cookies`
    :param human: 本会话的默认拟人档位。``False`` 全部直通，``True`` 用 DEFAULT
        预设，也可以直接给一个 :class:`~sleight.core.human.presets.HumanProfile`
    :param rng: 随机源。传固定 seed 的 :class:`random.Random` 可复现整段交互
    """

    def __init__(
        self,
        transport: Transport,
        session_id: str,
        target_id: str,
        *,
        owned_target: bool,
        track_network: bool = True,
        human: bool | HumanProfile = False,
        rng: Random | None = None,
    ) -> None:
        self._t = transport
        self._sid = session_id
        self._target_id = target_id
        self._owned = owned_target
        self._closed = False

        # 导航纪元：绑 loaderId，杜绝上一次导航的迟到事件满足这一次的等待
        self._loader_id: str | None = None
        self._frame_id: str | None = None
        self._lifecycle: set[str] = set()
        self._netidle = NetworkIdleTracker()
        self._track_network = track_network
        # 事件观察者。有了它就不必去 monkeypatch _handle —— 那是私有方法，而且补丁
        # 之间会互相覆盖
        self._observers: list[Callable[[Event], None]] = []

        self._t.call("Page.enable", session_id=self._sid)
        self._t.call("Runtime.enable", session_id=self._sid)
        self._t.call("Page.setLifecycleEventsEnabled", {"enabled": True}, session_id=self._sid)
        if track_network:
            self._t.call("Network.enable", session_id=self._sid)
        self.drain()

        self._input = InputDriver(self, default_human=human, rng=rng)

    # ------------------------------------------------------------------ #
    # 构造
    # ------------------------------------------------------------------ #

    @classmethod
    def create(cls, transport: Transport, **kw: Any) -> Session:
        """新建一个 ``about:blank`` tab 并接管它 —— 默认路径。

        落在默认 browser context 里，所以 Cookie 和登录态照样继承。

        :param transport: 浏览器级 WebSocket
        :param kw: 透传给构造函数（``human`` / ``rng`` / ``track_network``）
        :returns: ``owned_target=True`` 的 Session，退出时会关掉这个 tab
        """
        tid = transport.call("Target.createTarget", {"url": "about:blank"})["targetId"]
        try:
            sid = transport.call(
                "Target.attachToTarget", {"targetId": tid, "flatten": True}
            )["sessionId"]
            return cls(transport, sid, tid, owned_target=True, **kw)
        except BaseException:
            # 建了 target 却没接上，不能把它留在浏览器里泄漏
            try:
                transport.call("Target.closeTarget", {"targetId": tid})
            except SleightError:
                log.debug("could not clean up orphaned target %s", tid, exc_info=True)
            raise

    @classmethod
    def attach(cls, transport: Transport, target_id: str, **kw: Any) -> Session:
        """接管一个既有 tab —— 必须显式选择。

        :param transport: 浏览器级 WebSocket
        :param target_id: 要接管的 target id，可从
            :meth:`InstanceHandle.targets() <sleight.pool.InstanceHandle.targets>` 拿
        :param kw: 透传给构造函数
        :returns: ``owned_target=False`` 的 Session，退出时只 detach 不关闭
        """
        sid = transport.call(
            "Target.attachToTarget", {"targetId": target_id, "flatten": True}
        )["sessionId"]
        return cls(transport, sid, target_id, owned_target=False, **kw)

    def __enter__(self) -> Session:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def __repr__(self) -> str:
        own = "owned" if self._owned else "attached"
        return f"<Session {self._target_id[:8]} {own}{' closed' if self._closed else ''}>"

    # ------------------------------------------------------------------ #
    # 给 InputDriver 用的内部接口
    # ------------------------------------------------------------------ #

    @property
    def target_id(self) -> str:
        return self._target_id

    @property
    def owned_target(self) -> bool:
        return self._owned

    @property
    def transport(self) -> Transport:
        return self._t

    @property
    def cdp_session_id(self) -> str:
        return self._sid

    @property
    def closed(self) -> bool:
        return self._closed

    def call(self, method: str, params: dict[str, Any] | None = None, **kw: Any) -> dict[str, Any]:
        """发一条带本会话 sessionId 的 CDP 命令，等响应，并顺手排空事件。

        逃生舱 —— sleight 没封装的 CDP 命令直接从这里发。

        :param method: CDP 方法名，如 ``"Emulation.setGeolocationOverride"``
        :param params: 参数对象
        :param kw: 透传给 transport，目前只有 ``timeout``
        :returns: 响应的 ``result``
        :raises ProtocolError: CDP 返回了 error
        """
        result = self._t.call(method, params, session_id=self._sid, **kw)
        self.drain()
        return result

    def drain(self) -> None:
        """把**本会话**缓冲的事件喂给状态机。

        只取自己那一桶 —— 共用队列会让先 drain 的 Session 把别的 Session 的事件
        弹走并丢掉，对方就永远等不到自己的 ``load``。
        """
        for ev in self._t.drain_events(self._sid):
            self._handle(ev)

    def _handle(self, ev: Event) -> None:
        if ev.method == "Page.lifecycleEvent":
            # loaderId 绑定就是导航纪元 —— 上一次导航的 load 事件在这里被丢掉
            if self._loader_id is None or ev.params.get("loaderId") == self._loader_id:
                self._lifecycle.add(ev.params.get("name", ""))
        elif ev.method.startswith("Network."):
            self._netidle.feed(ev)

        for observe in self._observers:
            # 一个观察者抛异常不该把整条事件流搞停 —— 后面还有等待条件要靠它推进
            try:
                observe(ev)
            except Exception:
                log.warning("event observer %r raised; continuing", observe, exc_info=True)

    def _pump(self, timeout: float = _PUMP_SLICE) -> None:
        self._t.pump(timeout=timeout)
        self.drain()

    # ------------------------------------------------------------------ #
    # 导航与等待
    # ------------------------------------------------------------------ #

    def open(self, url: str, *, wait: Condition = _DEFAULT_WAIT, timeout: float = 60) -> None:
        """导航并等待条件满足。

        :param url: 目标地址
        :param wait: 等待条件，默认 :class:`~sleight.core.types.DomReady`。
            同文档导航（hash 路由）不产生任何 lifecycle 事件，此时
            ``DomReady`` / ``Load`` 直接返回，其余条件照常轮询
        :param timeout: 秒，覆盖导航命令和等待两段
        :raises TimeoutError: 条件没在 ``timeout`` 内满足
        :raises SleightError: 浏览器直接拒绝了这次导航（DNS 失败、协议错误等）
        """
        # 不走 self.call：必须先拿到新的 loaderId 再排空事件，否则新导航的 lifecycle
        # 事件会因为 loaderId 还是旧的而被当成迟到事件丢掉
        result = self._t.call("Page.navigate", {"url": url}, session_id=self._sid, timeout=timeout)
        if err := result.get("errorText"):
            raise SleightError(f"navigation to {url} failed: {err}")

        loader_id = result.get("loaderId")
        self._frame_id = result.get("frameId")

        if loader_id is None:
            # **同文档导航**（fragment / hash 路由）。Chrome 不会为它发任何 lifecycle
            # 事件，所以等 DomReady/Load 会一路等到超时。
            #
            # 也**不能**把 _loader_id 置成 None：_handle 里 None 的含义是"接受一切
            # loaderId"，那等于把导航纪元过滤器永久解除武装，之后每一次真导航都会被
            # 上一轮的迟到事件立刻满足。
            self.drain()
            if wait.kind in ("domready", "load"):
                return
            self.wait(wait, timeout=timeout)
            return

        # **先排空上一纪元的残留事件，再 reset。** `Page.navigate` 的响应回来之前，
        # 缓冲区里可能还压着旧文档的 Network.requestWillBeSent —— 顺序反过来的话
        # reset() 刚清空集合，紧接着的 drain() 又把它们原样塞回去，`NetworkIdle`
        # 会一直等一批永远不会结束的旧请求（只能靠 STALE_AFTER 兜底 15 秒）。
        # 这些事件必然属于旧纪元：新文档的加载在 loaderId 下发之后才开始。
        self.drain()

        self._loader_id = loader_id
        self._lifecycle.clear()
        self._netidle.reset(frame_id=self._frame_id)
        # 新纪元的早到事件（本次导航的 DOMContentLoaded 可能已经在队列里了）
        self.drain()

        self.wait(wait, timeout=timeout)

    def wait(self, cond: Condition, *, timeout: float = 30) -> None:
        """等一个条件成立。一次只收一个条件，要复合就连着调两次。

        轮询是脚本行为不是用户行为，不需要拟人化。

        :param cond: :class:`~sleight.core.types.DomReady` /
            :class:`~sleight.core.types.Load` / :class:`~sleight.core.types.Text` /
            :class:`~sleight.core.types.Selector` / :class:`~sleight.core.types.Gone` /
            :class:`~sleight.core.types.NetworkIdle`
        :param timeout: 秒
        :raises TimeoutError: 超时。异常的 ``last_value`` 带着最后一次求值结果
        :raises ValueError: 条件类型不认识
        """
        deadline = time.monotonic() + timeout
        poll = _POLL_MIN
        last: Any = None

        while True:
            self.drain()
            done, last = self._check(cond)
            if done:
                return
            if (remaining := deadline - time.monotonic()) <= 0:
                raise TimeoutError(
                    f"timed out after {timeout}s waiting for {cond}", last_value=last
                )
            # 事件类条件靠 pump 推进，轮询类条件靠 sleep + 重新求值
            self._pump(timeout=min(poll, remaining))
            poll = min(poll * 1.3, _POLL_MAX)

    def _check(self, cond: Condition) -> tuple[bool, Any]:
        kind = cond.kind
        if kind == "domready":
            return ("DOMContentLoaded" in self._lifecycle, sorted(self._lifecycle))
        if kind == "load":
            return ("load" in self._lifecycle, sorted(self._lifecycle))
        if kind == "text":
            body = self.eval("document.body ? document.body.innerText : ''") or ""
            return (cond.value in body, f"{len(body)} chars of innerText")  # type: ignore[attr-defined]
        if kind == "selector":
            n = self._count(cond.value)  # type: ignore[attr-defined]
            return (n > 0, n)
        if kind == "gone":
            n = self._count(cond.value)  # type: ignore[attr-defined]
            return (n == 0, n)
        if kind == "netidle":
            return (
                self._netidle.is_idle(cond.quiet),  # type: ignore[attr-defined]
                sorted(self._netidle.in_flight)[:5],
            )
        raise ValueError(f"unknown wait condition: {cond!r}")

    def _count(self, selector: str) -> int:
        return int(self.eval(f"document.querySelectorAll({json.dumps(selector)}).length") or 0)

    # ------------------------------------------------------------------ #
    # 事件与资源监听
    # ------------------------------------------------------------------ #

    @contextlib.contextmanager
    def observe_events(
        self, callback: Callable[[Event], None]
    ) -> Iterator[Callable[[Event], None]]:
        """临时挂一个原始 CDP 事件观察者。

        低层逃生舱 —— sleight 没建模的事件从这里拿。多数场景更该用
        :meth:`capture_resources`。

        观察者在 Session 自己的状态机（导航纪元、网络空闲）**之后**被调用，只收本会话
        的事件。它抛异常只会记一条 warning，不会打断事件流。

            >>> with session.observe_events(lambda ev: print(ev.method)):
            ...     session.open("https://example.com")

        :param callback: 收 :class:`~sleight.core.protocol.Event` 的可调用对象
        :returns: 上下文管理器，产出 ``callback`` 本身；退出时自动摘掉
        """
        self._observers.append(callback)
        try:
            yield callback
        finally:
            with contextlib.suppress(ValueError):
                self._observers.remove(callback)

    @contextlib.contextmanager
    def capture_resources(
        self,
        *,
        types: Iterable[str] | None = None,
        predicate: Callable[[NetworkResource], bool] | None = None,
        dedupe_by: DedupeKey | None = "url",
        on_discovered: Callable[[NetworkResource], None] | None = None,
    ) -> Iterator[ResourceTracker]:
        """收集页面加载过程中的网络资源。

        **库只负责给出结构化数据，筛选和输出格式是调用方的事** —— 所以这里没有任何
        打印，要看什么自己在 ``on_discovered`` 里写。

            >>> def show(r):
            ...     print(r.resource_type, r.url)
            >>> with session.capture_resources(
            ...     types={"Script", "Stylesheet"}, on_discovered=show
            ... ) as capture:
            ...     session.open(url, wait=Load())
            ...     session.pump_events(10)          # 等异步加载的那批
            >>> capture.urls("Script")
            [...]

        资源的分类可能到**响应回来**才确定（``requestWillBeSent`` 上的 ``type``
        经常缺失），所以 ``on_discovered`` 的时机是"首次满足筛选条件"，不是"首次
        看见"。

        看不到 attach 之前就已经发起的请求 —— 和
        :class:`~sleight.core.types.NetworkIdle` 是同一个语义边界。

        :param types: 只要这些 CDP ``ResourceType``（``Script`` / ``Stylesheet`` /
            ``XHR`` / ``Fetch`` / ``Image`` / ``Font`` / ``Document`` …）。``None`` = 全要
        :param predicate: 额外的自定义谓词，与 ``types`` 是**与**关系。
            例如 ``lambda r: r.status == 200``
        :param dedupe_by: ``"url"`` 每个地址只报一次（默认）；``"request_id"`` 每条
            请求一次；``None`` 不去重
        :param on_discovered: 首次匹配时的回调
        :returns: 上下文管理器，产出 :class:`~sleight.core.resources.ResourceTracker`。
            退出后 tracker 仍然可以 :meth:`~sleight.core.resources.ResourceTracker.snapshot`
        :raises ValueError: ``types`` 里有不认识的 ResourceType（拼错大小写的后果是
            静默抓不到东西，所以宁可直接报错）
        :raises SleightError: 本会话构造时 ``track_network=False``，没开 Network domain
        """
        if not self._track_network:
            raise SleightError(
                "capture_resources() needs the Network domain; this session was created "
                "with track_network=False"
            )
        tracker = ResourceTracker(
            types=frozenset(types) if types is not None else None,
            predicate=predicate,
            dedupe_by=dedupe_by,
            on_discovered=on_discovered,
        )
        with self.observe_events(tracker.feed):
            yield tracker

    def pump_events(self, duration: float, *, tick: float = 0.25) -> None:
        """原地收事件收 ``duration`` 秒，不判断任何条件。

        给"页面 load 完了，但还有一批 JS 在异步拉资源"这种场景用 —— 想等的是一段
        时间，不是一个条件。有明确条件时用 :meth:`wait`，别在这里睡固定时长。

        :param duration: 收多久，秒。``<= 0`` 直接返回
        :param tick: 单次 ``recv`` 的阻塞上限，秒。调小了响应更及时，也更占 CPU
        """
        if duration <= 0:
            return
        deadline = time.monotonic() + duration
        while (remaining := deadline - time.monotonic()) > 0:
            self._pump(timeout=min(tick, remaining))

    # ------------------------------------------------------------------ #
    # 读取
    # ------------------------------------------------------------------ #

    def eval(self, expr: str) -> Any:
        """``Runtime.evaluate``，返回 by-value 的结果。

        用 evaluate **读** DOM 是安全的（读不伪造事件）；**写交互不行** —— 那会产生
        ``isTrusted=false`` 且坐标 (0,0) 的假事件。点击一律走 :meth:`click`。

        :param expr: JS 表达式。拼用户输入进去时务必用 ``json.dumps`` 转义
        :returns: by-value 的求值结果；不可序列化的对象得到 ``None``
        :raises ProtocolError: JS 抛异常了
        """
        r = self.call(
            "Runtime.evaluate",
            {"expression": expr, "returnByValue": True, "awaitPromise": True},
        )
        if details := r.get("exceptionDetails"):
            desc = (details.get("exception") or {}).get("description") or details.get("text")
            raise ProtocolError(f"JS exception: {desc}")
        return (r.get("result") or {}).get("value")

    def content(self) -> str:
        """渲染后的 ``document.documentElement.outerHTML``。"""
        return self.eval("document.documentElement.outerHTML") or ""

    def text(self) -> str:
        """``document.body.innerText``。

        **与 HTML 文本不等价** —— 隐藏元素不计入，换行按渲染结果算。
        """
        return self.eval("document.body ? document.body.innerText : ''") or ""

    def title(self) -> str:
        """``document.title``。"""
        return self.eval("document.title") or ""

    def url(self) -> str:
        """``location.href`` —— 重定向之后的**当前**地址。"""
        return self.eval("location.href") or ""

    def viewport(self) -> tuple[int, int]:
        """实际可视区域，CSS 像素。

        以它为准而不是 framebuffer —— CloakBrowser 的 viewport 高度是
        ``screen_height − 133``，1080 的屏拿到的是 947。

        :returns: ``(innerWidth, innerHeight)``；页面还没法求值时回落到 ``(1280, 720)``
        """
        size = self.eval("[innerWidth, innerHeight]") or [1280, 720]
        return int(size[0]), int(size[1])

    # ------------------------------------------------------------------ #
    # 定位
    # ------------------------------------------------------------------ #

    def query(self, selector: str) -> Element | None:
        """第一个匹配的元素。

        **只支持主 frame 的普通 DOM**，不穿透 iframe / OOPIF / Shadow DOM。

        :param selector: CSS 选择器
        :returns: 元素；没命中返回 ``None``
        """
        el = Element(self, selector, 0)
        return el if el.exists() else None

    def query_all(self, selector: str) -> list[Element]:
        """所有匹配的元素。

        :param selector: CSS 选择器
        :returns: 按文档顺序排列；没命中返回空列表
        """
        return [Element(self, selector, i) for i in range(self._count(selector))]

    def require(self, target: Selectable) -> Element:
        """把选择器或 Element 统一成一个**确认存在**的 Element。

        :param target: CSS 选择器字符串，或已有的 Element
        :raises ElementError: 选择器没命中
        """
        el = Element(self, target, 0) if isinstance(target, str) else target
        if not el.exists():
            raise ElementError(f"no element matches {el.selector!r}[{el.index}]")
        return el

    # ------------------------------------------------------------------ #
    # 交互
    #
    # 全部委托给 InputDriver —— 它是唯一允许发输入事件的地方，也是维护鼠标位置
    # 连续性的地方。
    #
    # 每个方法的 human= 都是三态：None 继承 Session 默认，False 直通，
    # True 用 DEFAULT 预设，给 HumanProfile 就用它。
    # ------------------------------------------------------------------ #

    @property
    def cursor(self) -> Point:
        """当前鼠标位置。下一次移动从这里出发。

        首次访问时落在视口内的一个随机点，而不是 (0, 0)。
        """
        return self._input.cursor

    def click(
        self,
        target: Selectable | Point | Box,
        *,
        human: HumanSwitch = None,
        button: str = "left",
        click_count: int = 1,
    ) -> None:
        """移动过去并点击。元素不在视口里会先滚进来。

        按下之前会做**两次**命中校验（取到几何时一次、轨迹跑完时一次）—— 轨迹要跑
        300 ms 到 2 s，这期间冒出来的 cookie 横幅足以盖住目标。

        :param target: CSS 选择器 / :class:`~sleight.core.element.Element` /
            :class:`~sleight.core.types.Point` / :class:`~sleight.core.types.Box`。
            给裸坐标就不做命中校验
        :param human: 三态开关，``None`` 继承 Session 默认
        :param button: ``left`` / ``right`` / ``middle`` / ``back`` / ``forward``
        :param click_count: 连击次数
        :raises ElementError: 没命中、零尺寸、或落点被别的元素盖住
        """
        self._input.click(target, human=human, button=button, click_count=click_count)

    def double_click(self, target: Selectable | Point | Box, **kw: Any) -> None:
        """双击 —— **两次完整的按下抬起**，``detail`` 依次为 1、2。

        :param target: 同 :meth:`click`
        :param kw: 透传给 :meth:`click`（``human`` / ``button``）
        """
        self._input.click(target, click_count=2, **kw)

    def type(
        self,
        target: Selectable | None,
        text: str,
        *,
        human: HumanSwitch = None,
        clear: bool = False,
    ) -> None:
        """点击聚焦后逐字符输入。

        逐字符 keyDown/keyUp，间隔按 digraph 类型取分布。非 ASCII 成段走
        ``Input.insertText``（注意：它不产生 composition 事件，不等价于真 IME）。

        :param target: 先点它聚焦。``None`` 表示直接打到当前焦点上
        :param text: 要输入的文本
        :param human: 三态开关
        :param clear: 先 Ctrl+A 再 Backspace 清空。执行前会强制确认焦点真的落在
            ``target`` 上 —— 点一个 ``<div>`` 焦点会留在原处，那样清空的是别的输入框
        :raises ElementError: 目标不存在、被遮挡，或 ``clear=True`` 时焦点没落对
        """
        self._input.type(target, text, human=human, clear=clear)

    def press(self, key: str, *, human: HumanSwitch = None) -> None:
        """按一个键或组合键，打到当前焦点上。

        修饰键自己的 keyDown / keyUp 会真的发出去，不是只设 modifiers 位。

        :param key: ``"Enter"`` / ``"Ctrl+A"`` / ``"ctrl+shift+k"``，``+`` 分隔，
            大小写不敏感
        :param human: 三态开关，只影响按住时长
        :raises ValueError: 键名不认识
        """
        self._input.press(key, human=human)

    def scroll(self, dy: int, *, human: HumanSwitch = None) -> None:
        """在光标当前位置滚轮。滚的是**光标下面**那个容器。

        :param dy: 距离，px。正数向下
        :param human: 三态开关。拟人模式切成多个 80–160 px 的小步
        """
        self._input.scroll(dy, human=human)

    def scroll_into_view(self, target: Selectable, *, human: HumanSwitch = None) -> None:
        """把元素滚进视口。

        拟人模式发真实 ``mouseWheel`` 分步滚动；直通模式走
        ``DOM.scrollIntoViewIfNeeded``（瞬时，无滚轮事件）。只滚垂直方向。

        :param target: 选择器或 Element
        :param human: 三态开关
        :raises ElementError: 滚不动 —— 多半在嵌套滚动容器里，或横向出界
        """
        self._input.scroll_into_view(target, human=human)

    def hover(self, target: Selectable | Point | Box, *, human: HumanSwitch = None) -> None:
        """只移动光标过去，不按下。

        :param target: 同 :meth:`click`
        :param human: 三态开关
        """
        self._input.hover(target, human=human)

    # ------------------------------------------------------------------ #
    # 杂项
    # ------------------------------------------------------------------ #

    def screenshot(self, path: str | None = None) -> bytes:
        """整页截图，PNG。

        画面以**实际 viewport** 为准，不是 framebuffer。

        :param path: 给了就同时把字节写到这个文件
        :returns: PNG 字节
        """
        data = base64.b64decode(self.call("Page.captureScreenshot", {"format": "png"})["data"])
        if path:
            with open(path, "wb") as fh:
                fh.write(data)
        return data

    def cookies(self) -> list[dict[str, Any]]:
        """当前页面可见的 Cookie。需要构造时 ``track_network=True``（默认）。"""
        return self.call("Network.getCookies").get("cookies", [])

    def close(self) -> None:
        """关闭会话。幂等，可以重复调。

        自有 target 走 ``Target.closeTarget``（断 WebSocket 不等于关 tab，不关就是
        泄漏）；接管的只 ``Target.detachFromTarget``，那是别人的页面。

        关闭过程中的错误只记 debug 日志，不往外抛 —— 免得盖住调用方原本的异常。
        """
        if self._closed:
            return
        self._closed = True
        try:
            if self._owned:
                self._t.call("Target.closeTarget", {"targetId": self._target_id})
            else:
                self._t.call("Target.detachFromTarget", {"sessionId": self._sid})
        except SleightError:
            log.debug("error while closing session %r", self, exc_info=True)
