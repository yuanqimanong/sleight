from __future__ import annotations

import re
import shlex
import threading
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import Any

import pytest

from sleight.core import protocol
from sleight.core.element import Element
from sleight.core.errors import ProtocolError
from sleight.core.types import Box, Endpoint, InstanceInfo, InstanceStatus
from sleight.deploy.runner import CommandResult
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
        drag_data: dict[str, Any] | None = None,
        intercept_drags: bool = True,
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
        #: 非 None 就模拟一个 HTML5 原生可拖元素：按住移动时抛 Input.dragIntercepted
        self.drag_data = drag_data
        #: False 模拟老版本 Chrome —— 没有 Input.setInterceptDrags 这条命令
        self.intercept_drags = intercept_drags
        self.intercepting = False
        self._observers: list[Any] = []
        self._queued: list[protocol.Event] = []

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
        if (
            self.intercepting
            and self.drag_data is not None
            and not self._queued
            and method == "Input.dispatchMouseEvent"
            and params.get("type") == "mouseMoved"
            and params.get("buttons")
        ):
            # 真浏览器是异步抛的 —— 排进队列等 pump_events 取，别在 send 里直接回调
            self._queued.append(
                protocol.Event("Input.dragIntercepted", {"data": self.drag_data}, "SID")
            )

    # —— 事件观察 ——

    @contextmanager
    def observe_events(self, callback):
        self._observers.append(callback)
        try:
            yield callback
        finally:
            self._observers.remove(callback)

    def pump_events(self, duration: float, *, tick: float = 0.25) -> None:
        for event in self._queued:
            for callback in self._observers:
                callback(event)
        self._queued.clear()

    # —— Session 接口 ——

    def viewport(self) -> tuple[int, int]:
        return self._viewport

    def require(self, target) -> Element:
        return Element(self, target, 0) if isinstance(target, str) else target

    def drain(self) -> None:
        pass

    def call(self, method: str, params: dict | None = None, **kw):
        if method == "Input.setInterceptDrags":
            if not self.intercept_drags:
                raise ProtocolError("'Input.setInterceptDrags' wasn't found", code=-32601)
            self.intercepting = bool((params or {}).get("enabled"))
            return {}
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


# --------------------------------------------------------------------------- #
# 部署层的替身
# --------------------------------------------------------------------------- #


class FakeRunner:
    """一台假的目标机。

    自带一个极简的 POSIX 语义（``test`` / ``mkdir -p`` / ``cat`` / ``mv`` / ``rm -rf``
    和 ``sh -c 'cat > x'``），docker 之类的命令必须在 ``replies`` 里显式给 —— **没给的
    命令一律返回 127**。这一点是刻意的：engine 少发一条命令、多发一条命令，或者把
    ``docker inspect`` 的形状改了，测试都会当场变红，而不是悄悄走过去。
    """

    def __init__(
        self,
        *,
        label: str = "fake",
        files: dict[str, str] | None = None,
        dirs: Sequence[str] = (),
        replies: dict[str, tuple[int, str]] | None = None,
        unwritable: Sequence[str] = (),
        after_up: tuple[int, str] = (0, "running\thealthy\tcloakhq/cloakbrowser-manager:v0.0.10"),
    ) -> None:
        self.label = label
        self.files: dict[str, str] = dict(files or {})
        self.dirs: set[str] = set(dirs)
        self.replies: dict[str, tuple[int, str]] = dict(replies or {})
        self.unwritable = set(unwritable)
        #: 每一条执行过的命令，按顺序
        self.commands: list[tuple[str, ...]] = []
        self.sudo_commands: list[tuple[str, ...]] = []
        self.pushed: list[tuple[str, str]] = []
        self.streamed: list[tuple[str, ...]] = []
        self.modes: dict[str, int] = {}
        self.closed = False
        #: `docker compose up` 之后 `docker inspect` 该返回什么。替身也得有"启动"这个
        #: 因果，否则 wait_healthy 会对着一个永远 absent 的容器轮询到超时。
        #: 构造时显式给了 inspect 回复的测试以它为准，不做这个联动。
        self.after_up = after_up
        #: 权限检查（find ! -perm）返回几个 —— > 0 就是 chmod a+rX 没做
        self.unreadable_entries = 0
        self._pinned_inspect = "docker inspect --format" in (replies or {})
        for path in list(self.files):
            self._mkdirs(path.rsplit("/", 1)[0])

    # —— 断言辅助 ——

    def ran(self, *fragment: str) -> bool:
        """有没有执行过以 ``fragment`` 开头的命令。"""
        return any(cmd[: len(fragment)] == fragment for cmd in self.commands)

    def find(self, *fragment: str) -> list[tuple[str, ...]]:
        return [cmd for cmd in self.commands if cmd[: len(fragment)] == fragment]

    def count(self, *fragment: str) -> int:
        return len(self.find(*fragment))

    # —— Runner 接口 ——

    def run(
        self,
        argv: Sequence[str],
        *,
        stdin: bytes | None = None,
        cwd: str | None = None,
        timeout: float | None = None,
        sudo: bool = False,
        check: bool = False,
    ) -> CommandResult:
        argv = tuple(argv)
        self.commands.append(argv)
        if sudo:
            self.sudo_commands.append(argv)
        code, out = self._dispatch(argv, stdin)
        # 真的 CLI 失败时把原因写在 stderr 上，替身也照这个来 —— 否则测不到
        # preflight 是怎么解读 docker 的错误消息的
        err = "" if code == 0 else (out or f"fake: {argv[0]} failed")
        result = CommandResult(argv, code, out, err)
        return result.check() if check else result

    def _dispatch(self, argv: tuple[str, ...], stdin: bytes | None) -> tuple[int, str]:
        self._transition(argv)
        joined = " ".join(argv)
        for key in sorted(self.replies, key=len, reverse=True):
            if joined.startswith(key):
                return self.replies[key]

        match argv:
            case ("uname", *_):
                return 0, "Linux x86_64"
            case ("id", "-un"):
                return 0, "tester"
            case ("id", "-u"):
                return 0, "1000"
            case ("id", "-g"):
                return 0, "1000"
            case ("test", "-d", path):
                return (0, "") if path in self.dirs else (1, "")
            case ("test", "-e", path):
                return (0, "") if path in self.dirs or path in self.files else (1, "")
            case ("test", "-w", path):
                writable = path in self.dirs and path not in self.unwritable
                return (0, "") if writable else (1, "")
            case ("mkdir", "-p", path):
                self._mkdirs(path)
                return 0, ""
            case ("cat", path):
                return (0, self.files[path]) if path in self.files else (1, "")
            case ("mv", "-f", src, dst) | ("mv", src, dst):
                if src not in self.files:
                    return 1, ""
                self.files[dst] = self.files.pop(src)
                if src in self.modes:
                    self.modes[dst] = self.modes.pop(src)
                return 0, ""
            case ("chmod", mode, path):
                self.modes[path] = int(mode, 8)
                return 0, ""
            case ("chmod", "-R", *_) | ("chown", *_):
                return 0, ""
            case ("rm", "-rf", path):
                self.dirs.discard(path)
                for key in [k for k in self.files if k == path or k.startswith(path + "/")]:
                    del self.files[key]
                for key in [d for d in self.dirs if d.startswith(path + "/")]:
                    self.dirs.discard(key)
                return 0, ""
            case ("tar", *_):
                return 0, ""
            case ("sh", "-c", script):
                return self._shell(script, stdin)
        return 127, ""

    def _transition(self, argv: tuple[str, ...]) -> None:
        """compose 的启停改变容器状态 —— 这是引擎判断"要不要重建"的唯一依据。"""
        if self._pinned_inspect or argv[:2] != ("docker", "compose") or len(argv) < 3:
            return
        key = "docker inspect --format"
        match argv[2]:
            case "up" | "start":
                self.replies[key] = self.after_up
            case "stop":
                self.replies[key] = (0, "exited\tnone\tcloakhq/cloakbrowser-manager:v0.0.10")
            case "down":
                self.replies[key] = (1, "")

    def _shell(self, script: str, stdin: bytes | None) -> tuple[int, str]:
        if script.startswith("cat > "):
            path = shlex.split(script[len("cat > ") :])[0]
            self.files[path] = (stdin or b"").decode()
            self._mkdirs(path.rsplit("/", 1)[0])
            return 0, ""
        if script.startswith("ls -1 "):
            root = shlex.split(script[len("ls -1 ") :])[0]
            names = {
                d[len(root) + 1 :].split("/")[0]
                for d in list(self.dirs) + list(self.files)
                if d.startswith(root + "/")
            }
            return 0, "\n".join(sorted(names))
        if "! -perm" in script:                     # 权限检查：容器里的非 root 读不到几个
            return 0, str(self.unreadable_entries)
        if "-type f | wc -l" in script:
            root = shlex.split(script)[1]
            return 0, str(sum(1 for f in self.files if f.startswith(root + "/")))
        if "wc -l" in script:
            return 0, "0"
        return 0, ""

    def _mkdirs(self, path: str) -> None:
        parts = path.strip("/").split("/")
        for i in range(1, len(parts) + 1):
            self.dirs.add("/" + "/".join(parts[:i]))

    def read_text(self, path: str, *, sudo: bool = False) -> str | None:
        return self.files.get(path)

    def put_text(self, text: str, path: str, *, mode: int | None = None, sudo: bool = False) -> None:
        self.files[path] = text
        self._mkdirs(path.rsplit("/", 1)[0])
        if mode is not None:
            self.modes[path] = mode

    def put_dir(self, local: str, remote: str, *, sudo: bool = False) -> None:
        """真的把本地目录的内容搬进假文件系统 —— 这样 push 之后的校验才是真在校验。"""
        from pathlib import Path

        self.pushed.append((local, remote))
        self._mkdirs(remote)
        root = Path(local)
        for path in root.rglob("*"):
            if path.is_file():
                rel = path.relative_to(root).as_posix()
                self.files[f"{remote}/{rel}"] = path.read_text(encoding="utf-8", errors="replace")
                self._mkdirs(f"{remote}/{rel}".rsplit("/", 1)[0])

    def stream(self, argv: Sequence[str], *, cwd: str | None = None) -> int:
        self.streamed.append(tuple(argv))
        return 0

    @contextmanager
    def tunnel(self, remote_port: int, *, remote_host: str = "127.0.0.1") -> Iterator[int]:
        yield remote_port

    def close(self) -> None:
        self.closed = True


#: 一台 docker 一切正常、端口空闲、内存充足的机器
def docker_ok(
    *,
    container: str = "cloakbrowser-manager",
    listening: Sequence[int] = (22,),
    mounts: str = "",
    mem_kb: int = 16 * 1024 * 1024,
    avail_kb: int = 80 * 1024 * 1024,
    inspect: tuple[int, str] | None = None,
) -> dict[str, tuple[int, str]]:
    """给 :class:`FakeRunner` 用的一套 docker 探测回复。"""
    ss = "State Recv-Q Send-Q Local-Address:Port Peer-Address:Port\n" + "\n".join(
        f"LISTEN 0      4096   127.0.0.1:{p}          0.0.0.0:*" for p in listening
    )
    return {
        "docker version": (0, "27.3.1"),
        "docker compose version": (0, "2.29.7"),
        # 兜底：pull / up / down / stop / start / logs 一律成功。要测失败路径就在
        # 测试里覆盖更长的键（回复按键长度从长到短匹配）
        "docker compose": (0, ""),
        "docker logs": (0, ""),
        "docker rm": (0, ""),
        # 一次性 root 容器（备份和删 data/ 用它绕开 root 拥有的文件）
        "docker run": (0, ""),
        "docker image inspect": (1, ""),
        "docker ps -a --format": (0, mounts),
        "docker ps --filter": (0, ""),
        "ss -ltn": (0, ss),
        "cat /proc/meminfo": (0, f"MemTotal:       {mem_kb} kB\nMemFree: 100 kB\n"),
        "df -Pk": (0, f"Filesystem 1024-blocks Used Available Capacity Mounted on\n"
                      f"/dev/sda1 200000000 100 {avail_kb} 40% /"),
        "docker exec": (1, ""),
        "docker port": (1, ""),
        # inspect 刻意不给默认值：给了就等于把容器状态钉死，FakeRunner 的
        # up/stop/down 联动会失效，wait_healthy 只能轮询到超时
        **({"docker inspect --format": inspect} if inspect else {}),
    }


@pytest.fixture
def runner() -> FakeRunner:
    return FakeRunner(replies=docker_ok())
