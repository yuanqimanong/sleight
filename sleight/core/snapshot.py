"""Snapshot / Ref：把页面压成 LLM 能读的无障碍树，并给可交互元素稳定、可校验的 Ref。

这是"从自动化 SDK 变成 LLM 基座"的核心一层。设计对齐 Google chrome-devtools-mcp 的做法
（Apache-2.0）：Ref 内部由 ``(loaderId, backendNodeId)`` 复合键支撑 —— 同一节点跨快照拿到
**同一个** Ref，页面一导航（loaderId 变）旧 Ref 立刻失效（:class:`StaleRef`），绝不静默降级
去点一个可能不同的元素。

不做的事（有意，来自对既有实现的调研）：**不数 token**。三个成熟实现（playwright-mcp /
chrome-devtools-mcp / browser-use）都不在服务端算 token，而是用「结构蒸馏 + depth 上限 +
只给可交互节点 Ref」。这里照做——省一个 tokenizer 依赖，也不和某家分词器耦合。

Ref 解析出的 :class:`BackendElement` 满足 :class:`~sleight.core.element.ElementLike`，直接喂给
``session.click`` / ``type`` 就复用现有拟人轨迹 + 双重命中校验 + 真实输入（``isTrusted=true``），
一行输入代码都不用改——这正是先抽 ElementLike 的意义。

作用域（v1）：主 frame 的 AX 树。跨 frame（iframe/OOPIF）合并采集是明确的下一步，可复用
已建好的 frame 栈；本版对 iframe 内节点不发 Ref，不伪装已覆盖。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .errors import ElementError, ProtocolError, SleightError, StaleRef
from .types import Box

if TYPE_CHECKING:
    from .session import Session

__all__ = ["BackendElement", "Snapshot", "SnapshotNode"]

#: 会拿到 Ref 的可交互 AX role。结构性/纯文本节点不发 Ref，省得 LLM 上下文里全是噪声。
INTERACTABLE_ROLES = frozenset({
    "button", "link", "textbox", "searchbox", "combobox", "listbox", "checkbox",
    "radio", "menuitem", "menuitemcheckbox", "menuitemradio", "tab", "switch",
    "slider", "spinbutton", "option", "treeitem", "textfield",
})

#: 值得渲染进 Snapshot 文本的结构性 role（其余无名无 Ref 的节点会被折叠掉）。
_STRUCTURAL_KEEP = frozenset({
    "heading", "img", "list", "listitem", "table", "row", "cell", "columnheader",
    "rowheader", "navigation", "main", "article", "form", "dialog", "alert",
    "region", "banner", "contentinfo", "search",
})


@dataclass
class SnapshotNode:
    """Snapshot 树里的一个节点。

    :param role: AX role（``button`` / ``textbox`` / ``heading`` …）
    :param name: 可读名（accessible name）
    :param value: 表单类节点的当前值，可空
    :param ref: 可交互节点的不透明 Ref（如 ``"e5"``），否则 ``None``
    :param children: 子节点
    """

    role: str
    name: str
    value: str | None = None
    ref: str | None = None
    children: list[SnapshotNode] = field(default_factory=list)

    def render(self, *, _level: int = 0) -> str:
        """渲染成缩进的、LLM 友好的文本（每行 ``role "name" [ref]``）。"""
        lines: list[str] = []
        self._render_into(lines, 0)
        return "\n".join(lines)

    def _render_into(self, lines: list[str], level: int) -> None:
        label = self.role
        if self.name:
            label += f" {json.dumps(self.name, ensure_ascii=False)}"
        if self.value:
            label += f" value={json.dumps(self.value, ensure_ascii=False)}"
        if self.ref:
            label += f" [{self.ref}]"
        lines.append("  " * level + label)
        for child in self.children:
            child._render_into(lines, level + 1)


class Snapshot:
    """一次页面无障碍快照：一棵 :class:`SnapshotNode` + Ref → backendNodeId 映射。

    Ref 绑定取快照那一刻的导航纪元；页面之后又导航过，:meth:`ref` 抛 :class:`StaleRef`。
    """

    def __init__(
        self, session: Session, root: SnapshotNode, refs: dict[str, int], generation: str | None
    ) -> None:
        self._session = session
        self.root = root
        self._refs = refs                 # ref -> backendNodeId
        self.generation = generation       # 取快照时的 loaderId

    def __repr__(self) -> str:
        return f"<Snapshot {len(self._refs)} refs gen={self.generation}>"

    def text(self) -> str:
        """整棵树的 LLM 友好文本。"""
        return self.root.render()

    @property
    def refs(self) -> list[str]:
        """本快照里所有可交互元素的 Ref。"""
        return list(self._refs)

    def ref(self, ref: str) -> BackendElement:
        """把一个 Ref 解析成可交互的 :class:`BackendElement`。

        :param ref: :meth:`Session.snapshot` 里给出的不透明 Ref
        :raises StaleRef: 快照之后页面又导航过（纪元不符）
        :raises ElementError: Ref 不属于本快照
        """
        if self.generation != self._session._loader_id:
            raise StaleRef(
                f"ref {ref!r} is from a stale snapshot (page navigated since); take a new snapshot"
            )
        backend_node_id = self._refs.get(ref)
        if backend_node_id is None:
            raise ElementError(f"unknown ref {ref!r} in this snapshot")
        return BackendElement(self._session, backend_node_id, ref)


class BackendElement:
    """由 ``backendNodeId`` 支撑的 :class:`~sleight.core.element.ElementLike`。

    几何走 ``DOM.getBoxModel``（返回 viewport CSS 像素，正是输入用的坐标系）；命中/焦点/滚动
    度量走 ``DOM.resolveNode`` → ``Runtime.callFunctionOn``。因为满足 ElementLike，直接进现有
    InputDriver：拟人轨迹、双重命中校验、真实 CDP 输入一律照旧。
    """

    __slots__ = ("_backend_node_id", "_ref", "_session")

    def __init__(self, session: Session, backend_node_id: int, ref: str = "") -> None:
        self._session = session
        self._backend_node_id = backend_node_id
        self._ref = ref

    def __repr__(self) -> str:
        tag = f" {self._ref}" if self._ref else ""
        return f"<BackendElement backendNodeId={self._backend_node_id}{tag}>"

    # —— 解析 + 在节点上求值 —— #

    def _resolve(self) -> str | None:
        try:
            r = self._session.call("DOM.resolveNode", {"backendNodeId": self._backend_node_id})
        except ProtocolError:
            return None                     # 节点已不在（导航/移除）
        return (r.get("object") or {}).get("objectId")

    def _call(self, function_declaration: str, args: tuple[Any, ...] = ()) -> Any:
        """在本节点上（``this`` = 节点）调用一个 JS 函数，返回 by-value 结果。"""
        object_id = self._resolve()
        if object_id is None:
            raise ElementError(f"{self!r} is gone")
        try:
            r = self._session.call("Runtime.callFunctionOn", {
                "objectId": object_id,
                "functionDeclaration": function_declaration,
                "arguments": [{"value": a} for a in args],
                "returnByValue": True,
            })
            if details := r.get("exceptionDetails"):
                desc = (details.get("exception") or {}).get("description") or details.get("text")
                raise ProtocolError(f"JS exception: {desc}")
            return (r.get("result") or {}).get("value")
        finally:
            self._session.call("Runtime.releaseObject", {"objectId": object_id})

    # —— ElementLike 协议 —— #

    def exists(self) -> bool:
        object_id = self._resolve()
        if object_id is None:
            return False
        self._session.call("Runtime.releaseObject", {"objectId": object_id})
        return True

    def require_box(self) -> Box:
        try:
            r = self._session.call("DOM.getBoxModel", {"backendNodeId": self._backend_node_id})
        except ProtocolError as exc:
            raise ElementError(f"{self!r} is gone") from exc
        quad = (r.get("model") or {}).get("content")
        if not quad or len(quad) < 8:
            raise ElementError(f"{self!r} has no box (hidden?)")
        xs = quad[0::2]
        ys = quad[1::2]
        x, y = min(xs), min(ys)
        w, h = max(xs) - x, max(ys) - y
        box = Box(float(x), float(y), float(w), float(h))
        if box.empty:
            raise ElementError(f"{self!r} has zero size ({box.w}x{box.h}); is it hidden?")
        return box

    def in_viewport(self) -> bool:
        try:
            box = self.require_box()
        except ElementError:
            return False
        vw, vh = self._session.viewport()
        return box.x < vw and box.y < vh and box.x + box.w > 0 and box.y + box.h > 0

    def require_hit(self, x: int, y: int, *, when: str) -> None:
        # 用 DOM.getNodeForLocation：它原生穿透**同进程 iframe 和 shadow DOM**，直接返回该点
        # 最上层节点的 backendNodeId —— 比在某个文档里跑 elementFromPoint 更省事也更正确
        # （合并快照里 iframe 内元素的坐标是顶层坐标，用主文档 elementFromPoint 会命中 iframe
        # 本身而不是里面的元素）。命中的是本元素或其后代就算通过。
        try:
            loc = self._session.call(
                "DOM.getNodeForLocation",
                {"x": int(x), "y": int(y), "includeUserAgentShadowDOM": True},
            )
        except ProtocolError as exc:
            raise ElementError(f"{self!r}: nothing hit at ({x}, {y}) {when}") from exc
        hit = loc.get("backendNodeId")
        if hit == self._backend_node_id or (hit is not None and self._contains_backend(hit)):
            return
        raise ElementError(
            f"{self!r} is covered at ({x}, {y}) {when} (topmost node backendNodeId={hit})"
        )

    #: 从 o 沿 **composed 树**（assignedSlot / ShadowRoot→host）向上找 this —— 能跨 shadow
    #: 边界。用来判断"命中点落在本元素的 UA/author shadow 内部"（如 <input> 的内部编辑层、
    #: 投影进 slot 的内容），普通 ``this.contains(o)`` 跨不了 shadow 会漏判。
    _COMPOSED_CONTAINS = (
        "function(o){ let n=o; while(n){ if(n===this) return true;"
        " if(n.assignedSlot){ n=n.assignedSlot; continue; }"
        " const p=n.parentNode; n=(p&&p.nodeType===11&&p.host)?p.host:p; } return false; }"
    )

    def _contains_backend(self, other_backend_node_id: int) -> bool:
        """本元素是否 === 或（沿 composed 树）包含另一个 backendNodeId 的节点。"""
        target = self._resolve()
        if target is None:
            return False
        try:
            other = self._session.call("DOM.resolveNode", {"backendNodeId": other_backend_node_id})
            other_id = (other.get("object") or {}).get("objectId")
            if not other_id:
                return False
            try:
                r = self._session.call("Runtime.callFunctionOn", {
                    "objectId": target,
                    "functionDeclaration": self._COMPOSED_CONTAINS,
                    "arguments": [{"objectId": other_id}],
                    "returnByValue": True,
                })
                return bool((r.get("result") or {}).get("value"))
            finally:
                self._session.call("Runtime.releaseObject", {"objectId": other_id})
        except ProtocolError:
            return False
        finally:
            self._session.call("Runtime.releaseObject", {"objectId": target})

    def require_focus(self, *, after: str) -> None:
        fn = (
            "function() { const el = this; const a = el.ownerDocument.activeElement;"
            " return a === el || el.contains(a); }"
        )
        if not self._call(fn):
            raise ElementError(f"{self!r} does not have focus {after} — is it focusable?")

    def scroll_metrics(self) -> dict[str, float]:
        fn = (
            "function() { const r = this.getBoundingClientRect();"
            " const v = this.ownerDocument.defaultView || window;"
            " return {top: r.top, bottom: r.bottom, height: v.innerHeight}; }"
        )
        m = self._call(fn)
        if not m:
            raise ElementError(f"{self!r} is gone")
        return {"top": float(m["top"]), "bottom": float(m["bottom"]), "height": float(m["height"])}

    def object_id(self) -> str:
        object_id = self._resolve()
        if object_id is None:
            raise ElementError(f"{self!r} could not be resolved to a live node")
        return object_id


def _ax_str(node: dict[str, Any], key: str) -> str:
    return str((node.get(key) or {}).get("value") or "")


def _hoist(kids: list[SnapshotNode]) -> SnapshotNode | None:
    if not kids:
        return None
    if len(kids) == 1:
        return kids[0]
    return SnapshotNode(role="group", name="", children=kids)


def _child_frame_nodes(session: Session, iframe_backend_node_id: int) -> list[dict[str, Any]] | None:
    """取一个 iframe 的**同进程**子文档 AX 节点列表。

    对同源/同进程 frame，``Accessibility.getFullAXTree({frameId})`` 在主 session 上直接可取，
    且这些节点的 backendNodeId 在主 session 上就能解析、``DOM.getBoxModel`` 返回的还是**顶层
    坐标**（Chromium 已把同进程 frame 的偏移/滚动/transform 拍平）—— 所以 BackendElement 一行
    不用改就能点进去。跨源 OOPIF 在别的 target，这里取不到（返回 ``None``），留给
    :meth:`Session.frame_element` 走子 session。
    """
    try:
        node = session.call("DOM.describeNode", {"backendNodeId": iframe_backend_node_id})
        frame_id = (node.get("node") or {}).get("frameId")
        if not frame_id:
            return None
        return session.call("Accessibility.getFullAXTree", {"frameId": frame_id}).get("nodes") or []
    except SleightError:
        return None                          # OOPIF / 不可达：不下钻，iframe 节点当叶子


def build_snapshot(
    session: Session, nodes: list[dict[str, Any]], generation: str | None,
    *, ref_for: Any, max_depth: int | None, cross_frame: bool = True,
) -> Snapshot:
    """把 ``Accessibility.getFullAXTree`` 的扁平节点列表建成 :class:`Snapshot`。

    :param ref_for: ``backendNodeId -> ref`` 的分配器（由 Session 维护，保证跨快照稳定）
    :param max_depth: 最大深度；``None`` 不限
    :param cross_frame: 是否把同进程子 frame 的树合并进来（默认合并）。iframe 节点下就直接是
        它内部的可交互元素，Ref 照样能点，不用手动切 frame
    """
    refs: dict[str, int] = {}
    seen_frames: set[int] = set()            # 防自引用/重复下钻的兜底

    def build_tree(frame_nodes: list[dict[str, Any]], depth: int) -> SnapshotNode | None:
        by_id = {n["nodeId"]: n for n in frame_nodes}
        children_of = {n["nodeId"]: list(n.get("childIds") or []) for n in frame_nodes}
        root_id = next((n["nodeId"] for n in frame_nodes if not n.get("parentId")), None)

        def convert(node_id: str, node_depth: int, parent_name: str) -> SnapshotNode | None:
            node = by_id.get(node_id)
            if node is None:
                return None
            role = _ax_str(node, "role")
            name = _ax_str(node, "name")
            backend = node.get("backendDOMNodeId")

            # 蒸馏：InlineTextBox 永远是噪声；与父同名的 StaticText 是重复标签，去掉。
            if role == "InlineTextBox":
                return None
            if role == "StaticText" and name and name == parent_name:
                return None

            ref: str | None = None
            if role in INTERACTABLE_ROLES and backend is not None and not node.get("ignored"):
                ref = ref_for(backend)
                refs[ref] = backend

            kids: list[SnapshotNode] = []
            if max_depth is None or node_depth < max_depth:
                for child_id in children_of.get(node_id, []):
                    child = convert(child_id, node_depth + 1, name)
                    if child is not None:
                        kids.append(child)

            # 下钻同进程子 frame：把它内部的树接到这个 iframe 节点下
            if (
                cross_frame and role.lower() == "iframe" and backend is not None
                and backend not in seen_frames
                and (max_depth is None or node_depth < max_depth)
            ):
                seen_frames.add(backend)
                child_nodes = _child_frame_nodes(session, backend)
                if child_nodes:
                    sub = build_tree(child_nodes, node_depth + 1)
                    if sub is not None:
                        # 去掉子文档那层 RootWebArea 包裹，内容直接挂到 iframe 下
                        kids.extend(sub.children if sub.role == "RootWebArea" else [sub])

            generic = role in ("", "none", "presentation", "generic")
            if generic and not (ref or name):
                return _hoist(kids)
            keep = bool(ref) or bool(name) or role in _STRUCTURAL_KEEP or bool(kids)
            if not keep:
                return None
            value = _ax_str(node, "value") or None
            return SnapshotNode(
                role=role or "generic", name=name, value=value, ref=ref, children=kids
            )

        return convert(root_id, depth, "") if root_id is not None else None

    root = build_tree(nodes, 0) or SnapshotNode(role="RootWebArea", name="")
    return Snapshot(session, root, refs, generation)
