"""网络资源监听。

页面加载过程中每个请求的 URL、类型、状态码都在 ``Network.*`` 事件里，但这些事件的
信息是**分两批到的**：``requestWillBeSent`` 时 ``type`` 经常缺失或不准，只有
``responseReceived`` 带的 ``type`` 和 ``mimeType`` 才是权威的。

所以这里维护的是**可变记录 + 首次匹配才回调**：一个资源的分类可能在响应回来那一刻
才确定下来，回调要等到它**真的满足筛选条件**时才触发，而不是第一次看见它就触发。

这一层是 sans-io 的 —— 只吃 :class:`~sleight.core.protocol.Event`，不碰网络，
也**不负责打印**。库提供结构化的监听能力，筛选和输出格式由调用方决定。
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from typing import Literal

from .protocol import Event

__all__ = [
    "RESOURCE_TYPES",
    "DedupeKey",
    "NetworkResource",
    "ResourceTracker",
]

#: CDP ``Network.ResourceType`` 的全集。用来拦住 ``"StyleSheet"`` 这种拼写错误 ——
#: 大小写写错的后果是**静默什么都抓不到**，比报错难查得多。
RESOURCE_TYPES = frozenset({
    "Document", "Stylesheet", "Image", "Media", "Font", "Script", "TextTrack",
    "XHR", "Fetch", "Prefetch", "EventSource", "WebSocket", "Manifest",
    "SignedExchange", "Ping", "CSPViolationReport", "Preflight", "FedCM", "Other",
})

DedupeKey = Literal["url", "request_id"]


@dataclass(frozen=True, slots=True)
class NetworkResource:
    """页面加载过程中的一个网络资源。

    :ivar url: 请求地址。有重定向时是**最后**那一跳
    :ivar resource_type: CDP ``ResourceType``（``Script`` / ``Stylesheet`` / …）。
        响应还没回来时可能是空串
    :ivar request_id: CDP requestId。重定向复用同一个 id
    :ivar method: HTTP 方法
    :ivar mime_type: 响应的 Content-Type 主体部分。响应回来之前是空串
    :ivar status: HTTP 状态码。响应回来之前是 ``None``
    :ivar from_cache: 命中了磁盘或内存缓存
    :ivar finished: 收到过 ``loadingFinished``
    :ivar failed: 收到过 ``loadingFailed``
    :ivar error_text: 失败原因，如 ``net::ERR_ABORTED``
    :ivar size: 传输字节数（``encodedDataLength``）。未完成时是 ``None``
    :ivar redirects: 这条请求链上被重定向掉的历史 URL，按发生顺序
    """

    url: str
    resource_type: str = ""
    request_id: str = ""
    method: str = ""
    mime_type: str = ""
    status: int | None = None
    from_cache: bool = False
    finished: bool = False
    failed: bool = False
    error_text: str = ""
    size: float | None = None
    redirects: tuple[str, ...] = ()

    @property
    def pending(self) -> bool:
        """还在飞 —— 既没成功也没失败。"""
        return not self.finished and not self.failed

    def __str__(self) -> str:
        kind = self.resource_type or "?"
        state = "failed" if self.failed else ("ok" if self.finished else "pending")
        return f"{kind} {self.status or '-'} {state} {self.url}"


@dataclass(slots=True)
class _Filter:
    types: frozenset[str] | None
    predicate: Callable[[NetworkResource], bool] | None

    def matches(self, resource: NetworkResource) -> bool:
        if self.types is not None and resource.resource_type not in self.types:
            return False
        return self.predicate is None or self.predicate(resource)


@dataclass(slots=True)
class ResourceTracker:
    """喂 CDP ``Network.*`` 事件，攒出一份结构化的资源清单。

    :param types: 只保留这些 CDP ``ResourceType``。``None`` = 全要
    :param predicate: 额外的自定义谓词，和 ``types`` 是**与**关系
    :param dedupe_by: ``"url"`` 每个地址只报一次（默认，最符合"这个页面用了哪些 js"
        的直觉）；``"request_id"`` 每条请求报一次；``None`` 不去重
    :param on_discovered: 某个资源**首次满足筛选条件**时回调。分类可能到响应回来
        才确定，所以这个时机不等于"第一次看见它"
    :raises ValueError: ``types`` 里有不认识的 ResourceType
    """

    types: frozenset[str] | None = None
    predicate: Callable[[NetworkResource], bool] | None = None
    dedupe_by: DedupeKey | None = "url"
    on_discovered: Callable[[NetworkResource], None] | None = None

    _by_request: dict[str, NetworkResource] = field(default_factory=dict, init=False)
    _order: list[str] = field(default_factory=list, init=False)
    _announced: set[str] = field(default_factory=set, init=False)
    _filter: _Filter = field(init=False)

    def __post_init__(self) -> None:
        if self.types is not None:
            self.types = frozenset(self.types)
            if unknown := sorted(self.types - RESOURCE_TYPES):
                raise ValueError(
                    f"unknown CDP resource type(s): {unknown}. "
                    f"Valid values are {sorted(RESOURCE_TYPES)}"
                )
        self._filter = _Filter(self.types, self.predicate)

    # ------------------------------------------------------------------ #
    # 喂事件
    # ------------------------------------------------------------------ #

    def feed(self, event: Event) -> None:
        """喂一条 CDP 事件。非 ``Network.*`` 直接忽略。

        :param event: 解码后的 CDP 事件
        """
        method, p = event.method, event.params
        request_id = p.get("requestId")
        if not isinstance(request_id, str):
            return

        if method == "Network.requestWillBeSent":
            self._on_request(request_id, p)
        elif method == "Network.responseReceived":
            self._on_response(request_id, p)
        elif method == "Network.loadingFinished":
            self._update(request_id, finished=True, size=p.get("encodedDataLength"))
        elif method == "Network.loadingFailed":
            self._update(
                request_id,
                failed=True,
                error_text=str(p.get("errorText", "")),
                resource_type=str(p.get("type", "")) or None,
            )

    def _on_request(self, request_id: str, p: dict) -> None:
        request = p.get("request") or {}
        url = str(request.get("url", ""))

        if (existing := self._by_request.get(request_id)) is not None:
            # 重定向复用同一个 requestId：把上一跳记进历史，URL 指向新的一跳
            previous = existing.url
            self._replace(
                request_id,
                url=url or previous,
                redirects=(*existing.redirects, previous) if previous != url else existing.redirects,
                # 重定向的中间响应不代表最终结果，状态清回未决
                status=None,
                finished=False,
                failed=False,
            )
            return

        resource = NetworkResource(
            url=url,
            resource_type=str(p.get("type", "")),
            request_id=request_id,
            method=str(request.get("method", "")),
        )
        self._by_request[request_id] = resource
        self._order.append(request_id)
        self._announce(resource)

    def _on_response(self, request_id: str, p: dict) -> None:
        response = p.get("response") or {}
        # responseReceived 的 type 是权威的 —— requestWillBeSent 上的经常缺失
        self._update(
            request_id,
            resource_type=str(p.get("type", "")) or None,
            url=str(response.get("url", "")) or None,
            mime_type=str(response.get("mimeType", "")),
            status=int(response["status"]) if isinstance(response.get("status"), int) else None,
            from_cache=bool(response.get("fromDiskCache") or response.get("fromPrefetchCache")),
        )

    def _update(self, request_id: str, **changes: object) -> None:
        if request_id not in self._by_request:
            return                      # attach 之前就发起的请求，我们没有它的开头
        # None 表示"这个事件没带这个字段"，不要用它覆盖已有的值
        self._replace(request_id, **{k: v for k, v in changes.items() if v is not None})

    def _replace(self, request_id: str, **changes: object) -> None:
        updated = replace(self._by_request[request_id], **changes)  # type: ignore[arg-type]
        self._by_request[request_id] = updated
        self._announce(updated)

    def _announce(self, resource: NetworkResource) -> None:
        """首次满足筛选条件时回调一次。"""
        if not self._filter.matches(resource):
            return
        key = self._dedupe_key(resource)
        if key in self._announced:
            return
        self._announced.add(key)
        if self.on_discovered is not None:
            self.on_discovered(resource)

    def _dedupe_key(self, resource: NetworkResource) -> str:
        if self.dedupe_by == "url":
            return resource.url
        if self.dedupe_by == "request_id":
            return resource.request_id
        return f"{resource.request_id}\x00{resource.url}"

    # ------------------------------------------------------------------ #
    # 取结果
    # ------------------------------------------------------------------ #

    def snapshot(self) -> list[NetworkResource]:
        """当前匹配的资源，按首次出现的顺序。

        是**快照**不是视图 —— 拿到之后再来的事件不会改动它。
        """
        seen: set[str] = set()
        out: list[NetworkResource] = []
        for request_id in self._order:
            resource = self._by_request[request_id]
            if not self._filter.matches(resource):
                continue
            key = self._dedupe_key(resource)
            if key in seen:
                continue
            seen.add(key)
            out.append(resource)
        return out

    def urls(self, *types: str) -> list[str]:
        """只要地址。给了 ``types`` 就在筛选结果里再过一道。

        :param types: 可选的 ResourceType，如 ``urls("Script")``
        """
        wanted = frozenset(types) if types else None
        return [
            r.url for r in self.snapshot()
            if wanted is None or r.resource_type in wanted
        ]

    def by_type(self) -> dict[str, list[NetworkResource]]:
        """按 ResourceType 分组。"""
        grouped: dict[str, list[NetworkResource]] = {}
        for resource in self.snapshot():
            grouped.setdefault(resource.resource_type or "Other", []).append(resource)
        return grouped

    def reset(self) -> None:
        """清空全部状态。新导航开始时用。"""
        self._by_request.clear()
        self._order.clear()
        self._announced.clear()

    def __len__(self) -> int:
        return len(self.snapshot())

    def __iter__(self) -> Iterable[NetworkResource]:      # type: ignore[override]
        return iter(self.snapshot())
