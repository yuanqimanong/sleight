from __future__ import annotations

import re
import threading
import time
from typing import Any

import pytest

from sleight.core.element import Element
from sleight.core.types import Box, Endpoint, InstanceInfo, InstanceStatus
from sleight.providers.base import BaseProvider


class FakeProvider(BaseProvider):
    """N 个永远就绪的实例，可注入延迟和异常。"""

    def __init__(
        self,
        n: int = 3,
        *,
        name: str = "fake",
        delay: float = 0.0,
        fail: BaseException | None = None,
        tags: dict[str, set[str]] | None = None,
    ) -> None:
        self.name = name
        self.n = n
        self.delay = delay
        self.fail = fail
        self._tags = tags or {}
        self.ready_calls: list[str] = []
        self.release_calls: list[str] = []
        self.list_calls = 0

    def list_instances(self) -> list[InstanceInfo]:
        self.list_calls += 1
        if self.delay:
            time.sleep(self.delay)
        if self.fail is not None:
            raise self.fail
        return [
            InstanceInfo(
                id=f"i{k}", provider=self.name, ready=True,
                name=f"{self.name}-{k}", tags=frozenset(self._tags.get(f"i{k}", ())),
            )
            for k in range(self.n)
        ]

    def status(self, instance_id: str) -> InstanceStatus:
        idx = int(instance_id[1:]) if instance_id[1:].isdigit() else -1
        return InstanceStatus.RUNNING if 0 <= idx < self.n else InstanceStatus.NOT_FOUND

    def endpoint(self, instance_id: str | None = None) -> Endpoint:
        return Endpoint("http://fake", f"ws://fake/{instance_id}", {})

    def ensure_ready(self, instance_id: str) -> None:
        self.ready_calls.append(instance_id)

    def release(self, instance_id: str) -> None:
        self.release_calls.append(instance_id)


@pytest.fixture
def fake() -> FakeProvider:
    return FakeProvider()


class RecordingTransport:
    """记下每条命令走的是 ``send_no_wait`` 还是 ``call``。

    这个区分就是测试的重点：输入事件必须走 ``send_no_wait``，否则远程链路上一次点击
    要付 20–60 次 RTT。
    """

    def __init__(self, on_send=None) -> None:
        self.sent: list[tuple[str, dict]] = []      # send_no_wait
        self.called: list[tuple[str, dict]] = []    # call（等响应）
        self.flushes = 0
        self.closed = False
        self._on_send = on_send

    def send_no_wait(self, method, params=None, *, session_id=None):
        params = params or {}
        self.sent.append((method, params))
        if self._on_send is not None:
            self._on_send(method, params)
        return len(self.sent)

    def call(self, method, params=None, *, session_id=None, **kw):
        self.called.append((method, params or {}))
        return {}

    def flush(self, **kw) -> None:
        self.flushes += 1

    def drain_events(self):
        return iter(())

    def pump(self, *, timeout: float) -> bool:
        return False

    def close(self) -> None:
        self.closed = True

    # —— 断言辅助 ——

    def types(self) -> list[str]:
        """按顺序列出发出去的鼠标/键盘事件类型。"""
        out = []
        for method, params in self.sent:
            if method == "Input.insertText":
                out.append("insertText")
            else:
                out.append(params.get("type", method))
        return out


class FakeSession:
    """够 InputDriver 和 Element 用的最小 Session 替身。

    ``eval`` 按 JS 片段的特征串路由 —— 这样测的是 Element 真正拼出来的 JS，
    而不是一个理想化的接口。

    **它会真的滚动。** 元素位置存在文档坐标系里（``doc_box``），视口坐标 = 文档坐标
    − ``scroll_y``，而 ``scroll_y`` 由发出去的 ``mouseWheel`` 事件驱动。这一点是必需的：
    box 不随滚动变化的替身会让"滚完重新取 box"和"in_viewport 才算成功"
    这两条完全失去保护 —— 把成功判据写错成"垂直偏移为 0"也照样绿。
    """

    def __init__(
        self,
        *,
        box: Box | None = None,
        viewport: tuple[int, int] = (1280, 720),
        hits: list[bool] | bool = True,
        exists: bool = True,
        focused: bool = True,
        scrollable: bool = True,
    ) -> None:
        self.transport = RecordingTransport(self._observe)
        self.cdp_session_id = "SID"
        #: 文档坐标系里的位置。默认就在视口内。
        self.doc_box = box if box is not None else Box(400.0, 300.0, 120.0, 40.0)
        self.scroll_y = 0.0
        self.scrollable = scrollable          # False 模拟嵌套滚动容器吃掉滚轮
        self._viewport = viewport
        self._hits = hits
        self._exists = exists
        self.focused = focused
        self.evals: list[str] = []
        self.hit_tests = 0
        self.probes: list[tuple[int, int]] = []      # 命中校验探到的坐标
        self.scrolled_instantly = 0
        self.released_objects = 0

    # —— 几何 ——

    @property
    def box(self) -> Box:
        """视口坐标系里的 box。"""
        return Box(self.doc_box.x, self.doc_box.y - self.scroll_y, self.doc_box.w, self.doc_box.h)

    def _observe(self, method: str, params: dict) -> None:
        if (
            self.scrollable
            and method == "Input.dispatchMouseEvent"
            and params.get("type") == "mouseWheel"
        ):
            self.scroll_y += params.get("deltaY", 0)

    # —— Session 接口 ——

    def viewport(self) -> tuple[int, int]:
        return self._viewport

    def require(self, target) -> Element:
        return Element(self, target, 0) if isinstance(target, str) else target

    def drain(self) -> None:
        pass

    def call(self, method: str, params: dict | None = None, **kw):
        if method == "DOM.scrollIntoViewIfNeeded":
            self.scrolled_instantly += 1
            self.scroll_y = self.doc_box.y - self._viewport[1] * 0.4
            return {}
        if method == "Runtime.releaseObject":
            self.released_objects += 1
            return {}
        if method == "Runtime.evaluate" and not (params or {}).get("returnByValue", True):
            return {"result": {"objectId": "OBJ-1"}}
        return {}

    def eval(self, expr: str):
        self.evals.append(expr)
        box, (vw, vh) = self.box, self._viewport

        if "elementFromPoint" in expr and "el.contains(hit)" in expr:
            match = re.search(r"elementFromPoint\((-?\d+),\s*(-?\d+)\)", expr)
            if match:
                self.probes.append((int(match.group(1)), int(match.group(2))))
            self.hit_tests += 1
            if isinstance(self._hits, bool):
                return self._hits
            index = self.hit_tests - 1
            return self._hits[index] if index < len(self._hits) else self._hits[-1]
        if "h.tagName" in expr:                       # 被遮挡时的元素描述
            return "div#cookie-banner"
        if "activeElement" in expr and "el.contains" in expr:
            return self.focused
        if "activeElement" in expr:                   # require_focus 的诊断串
            return "body"
        if "return {top:" in expr:                    # scroll_metrics
            return {"top": box.y, "bottom": box.y + box.h, "height": float(vh)}
        if "return {x:" in expr:                      # box
            return {"x": box.x, "y": box.y, "w": box.w, "h": box.h}
        if "r.bottom > 0" in expr:                    # in_viewport
            return box.y + box.h > 0 and box.x + box.w > 0 and box.y < vh and box.x < vw
        if "return true;" in expr:                    # exists
            return self._exists
        if "querySelectorAll" in expr and ".length" in expr:
            return 1
        return None


@pytest.fixture
def session() -> FakeSession:
    return FakeSession()


def run_threads(fn: Any, n: int, timeout: float = 30.0) -> list[BaseException]:
    """并发跑 n 次，返回抛出的异常。"""
    errors: list[BaseException] = []
    lock = threading.Lock()

    def wrapped() -> None:
        try:
            fn()
        except BaseException as exc:
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=wrapped) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout)
    return errors
