"""四工具 Gateway：给 LLM / MCP 的稳定、类型化、结果结构化的浏览器操作面。

四个工具（对齐方案里的 Session / Observe / Act / Extract）+ 一个 ``find``：

- **session**：导航与生命周期（open / reload / back / forward / info）。
- **observe**：``snapshot``（把页面压成 LLM 可读文本 + 可交互元素 Ref）与 ``find``
  （按文字/role 在快照里定位元素，返回它们的 Ref）。
- **act**：click / type / press / scroll / hover —— **既收 Ref 也收坐标**（坐标是留给
  Canvas/图标类 UI 的逃生舱，ref-only 会在那类界面上硬失败）。
- **extract**：正文与常用字段。

设计取舍（来自对既有实现的调研）：

- **默认不暴露 raw CDP / eval**。要用得显式 ``allow_raw=True``，且走单独的高权限入口，
  页面内容永远无法扩大能力。
- **硬上限放在服务端**（Gateway/Session），不放在模型提交的请求里 —— 请求里的"限额"
  等于没有限额。
- 失败返回结构化 :class:`ToolResult`（``ok=False`` + ``error``），不往模型抛异常；Ref 因
  导航失效时错误里直接提示"重拍快照"。

这是 Python 层的工具面；包成 MCP server / HTTP 只是把每个 action 映射成一个 tool schema，
:meth:`Gateway.describe` 给出这份目录。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ..core.errors import SleightError
from ..core.types import DomReady, Point, Selector, Text

if TYPE_CHECKING:
    from ..core.session import Session
    from ..core.snapshot import Snapshot

__all__ = ["Gateway", "ToolResult"]

_SESSION_ACTIONS = ("open", "reload", "back", "forward", "info")
_OBSERVE_ACTIONS = ("snapshot", "find")
_ACT_ACTIONS = ("click", "type", "press", "scroll", "hover")


@dataclass
class ToolResult:
    """一次工具调用的结构化结果。失败也是它（``ok=False`` + ``error``），不抛异常。"""

    ok: bool
    tool: str
    action: str
    data: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    def __bool__(self) -> bool:
        return self.ok

    @classmethod
    def fail(cls, tool: str, action: str, error: str) -> ToolResult:
        return cls(ok=False, tool=tool, action=action, error=error)


class Gateway:
    """把一个 :class:`~sleight.core.session.Session` 包成给 LLM/MCP 的四工具面。

    :param session: 已连上的 Session
    :param max_batch: 预留的批量上限（服务端强制，模型改不了）
    :param allow_raw: 是否放开 raw CDP/eval 高权限入口，默认关
    """

    def __init__(self, session: Session, *, max_batch: int = 5, allow_raw: bool = False) -> None:
        self._s = session
        self.max_batch = max_batch
        self.allow_raw = allow_raw
        self._snapshot: Snapshot | None = None

    # ------------------------------------------------------------------ #
    # Session 工具
    # ------------------------------------------------------------------ #

    def session(
        self, action: str, *, url: str | None = None,
        wait_text: str | None = None, wait_selector: str | None = None, timeout: float = 30.0,
    ) -> ToolResult:
        """导航与生命周期。action ∈ ``open`` / ``reload`` / ``back`` / ``forward`` / ``info``。"""
        if action not in _SESSION_ACTIONS:
            return ToolResult.fail("session", action, f"unknown action; use {_SESSION_ACTIONS}")
        wait = self._wait_cond(wait_text, wait_selector)
        try:
            if action == "open":
                if not url:
                    return ToolResult.fail("session", action, "open needs a url")
                self._s.open(url, wait=wait, timeout=timeout)
            elif action == "reload":
                self._s.reload(wait=wait, timeout=timeout)
            elif action == "back":
                self._s.back(timeout=timeout)
            elif action == "forward":
                self._s.forward(timeout=timeout)
            self._snapshot = None            # 页面变了，旧快照作废
            return ToolResult(True, "session", action,
                              {"url": self._s.url(), "title": self._s.title()})
        except SleightError as exc:
            return ToolResult.fail("session", action, str(exc))

    # ------------------------------------------------------------------ #
    # Observe 工具
    # ------------------------------------------------------------------ #

    def observe(
        self, action: str = "snapshot", *, query: str | None = None,
        role: str | None = None, max_depth: int | None = None,
    ) -> ToolResult:
        """观察页面。``snapshot`` 拍快照（文本 + Ref）；``find`` 按文字/role 找元素。"""
        if action not in _OBSERVE_ACTIONS:
            return ToolResult.fail("observe", action, f"unknown action; use {_OBSERVE_ACTIONS}")
        try:
            if action == "snapshot":
                self._snapshot = self._s.snapshot(max_depth=max_depth)
                return ToolResult(True, "observe", action,
                                  {"snapshot": self._snapshot.text(), "refs": self._snapshot.refs})
            # find：在（必要时新拍的）快照里按 name 子串 + 可选 role 匹配
            if self._snapshot is None:
                self._snapshot = self._s.snapshot(max_depth=max_depth)
            matches = self._find(query, role)
            return ToolResult(True, "observe", action, {"matches": matches})
        except SleightError as exc:
            return ToolResult.fail("observe", action, str(exc))

    def find(self, query: str, *, role: str | None = None) -> ToolResult:
        """``observe(action="find")`` 的便捷别名：按文字（可选 role）定位可交互元素。"""
        return self.observe("find", query=query, role=role)

    # ------------------------------------------------------------------ #
    # Act 工具
    # ------------------------------------------------------------------ #

    def act(
        self, action: str, *, ref: str | None = None,
        coordinate: tuple[int, int] | None = None, text: str | None = None,
        key: str | None = None, dy: int = 0, human: bool = True,
    ) -> ToolResult:
        """操作元素。action ∈ ``click`` / ``type`` / ``press`` / ``scroll`` / ``hover``。

        目标既可以是 ``ref``（来自 observe 的快照），也可以是 ``coordinate``（Canvas/图标 UI
        的逃生舱）。``press``/``scroll`` 不需要目标。
        """
        if action not in _ACT_ACTIONS:
            return ToolResult.fail("act", action, f"unknown action; use {_ACT_ACTIONS}")
        try:
            if action == "press":
                if not key:
                    return ToolResult.fail("act", action, "press needs a key")
                self._s.press(key, human=human)
                return ToolResult(True, "act", action, {"key": key})
            if action == "scroll":
                self._s.scroll(dy, human=human)
                return ToolResult(True, "act", action, {"dy": dy})

            target = self._target(ref, coordinate)
            if action == "click":
                self._s.click(target, human=human)
            elif action == "hover":
                self._s.hover(target, human=human)
            elif action == "type":
                if text is None:
                    return ToolResult.fail("act", action, "type needs text")
                self._s.type(target, text, human=human)
            return ToolResult(True, "act", action, {"ref": ref, "coordinate": coordinate})
        except SleightError as exc:
            return ToolResult.fail("act", action, str(exc))

    # ------------------------------------------------------------------ #
    # Extract 工具
    # ------------------------------------------------------------------ #

    def extract(self, *, min_length: int = 200) -> ToolResult:
        """抽取正文与常用字段。"""
        try:
            doc = self._s.extract_document(min_length=min_length)
            return ToolResult(True, "extract", "document", {
                "title": doc.title, "text": doc.text, "byline": doc.byline,
                "excerpt": doc.excerpt, "lang": doc.lang, "links": doc.links,
                "metadata": doc.metadata, "json_ld": doc.json_ld,
                "quality": doc.quality, "low_quality": doc.low_quality,
            })
        except SleightError as exc:
            return ToolResult.fail("extract", "document", str(exc))

    # ------------------------------------------------------------------ #
    # 高权限逃生舱（默认关）
    # ------------------------------------------------------------------ #

    def eval(self, expr: str) -> ToolResult:
        """Raw ``Runtime.evaluate`` —— **仅** ``allow_raw=True`` 时可用。页面内容永远开不了它。"""
        if not self.allow_raw:
            return ToolResult.fail("raw", "eval", "raw eval is disabled (allow_raw=False)")
        try:
            return ToolResult(True, "raw", "eval", {"value": self._s.eval(expr)})
        except SleightError as exc:
            return ToolResult.fail("raw", "eval", str(exc))

    # ------------------------------------------------------------------ #
    # 工具目录（映射到 MCP / 系统提示用）
    # ------------------------------------------------------------------ #

    def describe(self) -> dict[str, list[str]]:
        """列出工具与它们的 action —— 拿去生成 MCP tool schema 或系统提示。"""
        return {
            "session": list(_SESSION_ACTIONS),
            "observe": list(_OBSERVE_ACTIONS),
            "act": list(_ACT_ACTIONS),
            "extract": ["document"],
        }

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #

    def _wait_cond(self, wait_text: str | None, wait_selector: str | None) -> Any:
        if wait_selector:
            return Selector(wait_selector)
        if wait_text:
            return Text(wait_text)
        return DomReady()

    def _target(self, ref: str | None, coordinate: tuple[int, int] | None) -> Any:
        if ref is not None:
            if self._snapshot is None:
                raise SleightError("no snapshot yet — call observe(snapshot) before using a ref")
            return self._snapshot.ref(ref)          # 可能抛 StaleRef，会被上层转成结构化错误
        if coordinate is not None:
            return Point(int(coordinate[0]), int(coordinate[1]))
        raise SleightError("act needs a ref or a coordinate")

    def _find(self, query: str | None, role: str | None) -> list[dict[str, Any]]:
        assert self._snapshot is not None
        needle = (query or "").casefold()
        out: list[dict[str, Any]] = []

        def walk(node: Any) -> None:
            if node.ref and (role is None or node.role == role) and (
                not needle or needle in node.name.casefold()
            ):
                out.append({"ref": node.ref, "role": node.role, "name": node.name})
            for child in node.children:
                walk(child)

        walk(self._snapshot.root)
        return out

    @property
    def snapshot(self) -> Snapshot | None:
        """最近一次 observe 拍到的快照（供 act 的 ref 解析）。"""
        return self._snapshot
