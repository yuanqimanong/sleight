"""把 :class:`~sleight.agent.gateway.Gateway` 暴露成一个 MCP server（stdio 传输）。

MCP（Model Context Protocol）就是 **JSON-RPC 2.0 over stdio**：客户端（Claude Desktop /
IDE / 任意 MCP host）逐行发 JSON-RPC 请求，server 逐行回。这里**零依赖**手写协议（不引
官方 mcp SDK），因此和本库其它部分一样只依赖标准库，也便于离线测试。

暴露的工具就是 Gateway 的四工具面：``browser_session`` / ``browser_observe`` /
``browser_act`` / ``browser_extract``。结果里带快照文本或结构化数据；工具级失败走
``isError`` 而不是 JSON-RPC error（协议错误才用后者）。

跑法：

    SLEIGHT_CDP_URL=http://127.0.0.1:9222 python -m sleight.agent.mcp
    # 或让它自己起本机浏览器：
    SLEIGHT_BROWSER=fingerprint-chromium SLEIGHT_FINGERPRINT=42 python -m sleight.agent.mcp

stdout 是协议通道，**只**写 JSON-RPC；日志一律走 stderr。
"""

from __future__ import annotations

import json
import logging
import os
import sys
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .gateway import Gateway

__all__ = ["MCPServer", "main"]

log = logging.getLogger("sleight.mcp")

_PROTOCOL_VERSION = "2024-11-05"

# 一个坐标 [x, y] 的 schema，act 的逃生舱用
_COORD = {"type": "array", "items": {"type": "integer"}, "minItems": 2, "maxItems": 2}

TOOL_SPECS: list[dict[str, Any]] = [
    {
        "name": "browser_session",
        "description": "Navigate / lifecycle. action=open needs url; reload/back/forward take no "
                       "target. Optionally wait for wait_text or wait_selector.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["open", "reload", "back", "forward", "info"]},
                "url": {"type": "string"},
                "wait_text": {"type": "string"},
                "wait_selector": {"type": "string"},
                "timeout": {"type": "number"},
            },
            "required": ["action"],
        },
    },
    {
        "name": "browser_observe",
        "description": "Observe the page. action=snapshot returns an LLM-readable accessibility "
                       "tree with a [ref] on each interactable element; action=find returns the "
                       "refs whose name matches query (optionally filtered by role).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["snapshot", "find"], "default": "snapshot"},
                "query": {"type": "string"},
                "role": {"type": "string"},
                "max_depth": {"type": "integer"},
            },
        },
    },
    {
        "name": "browser_act",
        "description": "Act on an element. Target it by ref (from observe) or by coordinate "
                       "[x,y] (escape hatch for canvas/icon UIs). click/type/hover need a target; "
                       "type needs text; press needs key; scroll needs dy.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string",
                           "enum": ["click", "type", "press", "scroll", "hover"]},
                "ref": {"type": "string"},
                "coordinate": _COORD,
                "text": {"type": "string"},
                "key": {"type": "string"},
                "dy": {"type": "integer"},
                "human": {"type": "boolean", "default": True},
            },
            "required": ["action"],
        },
    },
    {
        "name": "browser_extract",
        "description": "Extract the main content and common fields (title, byline, excerpt, "
                       "metadata, JSON-LD, links) of the current page.",
        "inputSchema": {
            "type": "object",
            "properties": {"min_length": {"type": "integer", "default": 200}},
        },
    },
]


class MCPServer:
    """把一个 Gateway（懒创建）包成 MCP server。协议处理与传输分离，便于单测。

    :param gateway_provider: 无参、返回一个 :class:`Gateway` 的工厂。懒调用 —— ``initialize``
        / ``tools/list`` 不需要浏览器，真正用到时（第一个 ``tools/call``）才连
    :param name: server 名，握手时报给客户端
    """

    def __init__(self, gateway_provider: Callable[[], Gateway], *, name: str = "sleight") -> None:
        self._provider = gateway_provider
        self._gateway: Gateway | None = None
        self._name = name

    def _gw(self) -> Gateway:
        if self._gateway is None:
            self._gateway = self._provider()
        return self._gateway

    # ------------------------------------------------------------------ #
    # 协议处理（纯函数式，喂一条消息出一条响应）
    # ------------------------------------------------------------------ #

    def handle(self, message: dict[str, Any]) -> dict[str, Any] | None:
        """处理一条 JSON-RPC 消息。通知（无 id / notifications.*）返回 ``None``（不回）。"""
        method = message.get("method")
        mid = message.get("id")
        params = message.get("params") or {}

        if isinstance(method, str) and method.startswith("notifications/"):
            return None                      # 通知不回

        try:
            if method == "initialize":
                result = self._initialize(params)
            elif method == "tools/list":
                result = {"tools": TOOL_SPECS}
            elif method == "tools/call":
                result = self._tools_call(params)
            elif method == "ping":
                result = {}
            else:
                return _error(mid, -32601, f"method not found: {method}")
        except Exception as exc:             # 兜底：别让一条坏消息掀翻 server
            log.exception("error handling %s", method)
            return _error(mid, -32603, f"internal error: {exc}")

        if mid is None:
            return None
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    def _initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        from .. import __version__
        return {
            "protocolVersion": params.get("protocolVersion") or _PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": self._name, "version": __version__},
        }

    def _tools_call(self, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name")
        args = params.get("arguments") or {}
        gw = self._gw()

        if name == "browser_session":
            r = gw.session(
                args.get("action", "info"), url=args.get("url"),
                wait_text=args.get("wait_text"), wait_selector=args.get("wait_selector"),
                timeout=args.get("timeout", 30.0),
            )
        elif name == "browser_observe":
            r = gw.observe(
                args.get("action", "snapshot"), query=args.get("query"),
                role=args.get("role"), max_depth=args.get("max_depth"),
            )
        elif name == "browser_act":
            coord = args.get("coordinate")
            r = gw.act(
                args.get("action", ""), ref=args.get("ref"),
                coordinate=tuple(coord) if coord else None,
                text=args.get("text"), key=args.get("key"),
                dy=args.get("dy", 0), human=args.get("human", True),
            )
        elif name == "browser_extract":
            r = gw.extract(min_length=args.get("min_length", 200))
        else:
            return {"content": [_text(f"unknown tool: {name}")], "isError": True}

        return _result_to_call(r)

    # ------------------------------------------------------------------ #
    # stdio 传输
    # ------------------------------------------------------------------ #

    def serve_stdio(self, stdin: Any = None, stdout: Any = None) -> None:
        """按行读 JSON-RPC，按行回。stdout 只写协议，日志走 stderr。"""
        stdin = stdin or sys.stdin
        stdout = stdout or sys.stdout
        for line in stdin:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                self._write(stdout, _error(None, -32700, "parse error"))
                continue
            response = self.handle(message)
            if response is not None:
                self._write(stdout, response)

    @staticmethod
    def _write(stdout: Any, obj: dict[str, Any]) -> None:
        stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
        stdout.flush()


# --------------------------------------------------------------------------- #
# 工具结果 -> MCP content
# --------------------------------------------------------------------------- #


def _text(text: str) -> dict[str, Any]:
    return {"type": "text", "text": text}


def _result_to_call(result: Any) -> dict[str, Any]:
    """把 :class:`ToolResult` 转成 MCP tools/call 结果。工具级失败走 isError。"""
    if not result.ok:
        return {"content": [_text(f"error: {result.error}")], "isError": True}
    data = result.data
    if "snapshot" in data:                   # 快照直接给文本，别 JSON 转义一大坨
        text = data["snapshot"]
        if data.get("refs"):
            text += "\n\nrefs: " + ", ".join(data["refs"])
    else:
        text = json.dumps(data, ensure_ascii=False, indent=2)
    return {"content": [_text(text)], "isError": False}


def _error(mid: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #


_NO_BROWSER_HINT = (
    "set SLEIGHT_CDP_URL=http://host:port (existing browser) or "
    "SLEIGHT_BROWSER=<binary> (launch one) before starting the MCP server"
)


def _check_env() -> None:
    """启动时就确认配了浏览器来源。

    **不能等到第一次 tools/call 才报** —— Gateway 是懒创建的，MCP host 里配错环境变量
    会表现成"server 起来了、什么都不说、然后静默退出"，日志里毫无线索。宁可启动即失败。
    """
    if not (os.environ.get("SLEIGHT_CDP_URL") or os.environ.get("SLEIGHT_BROWSER")):
        raise SystemExit(_NO_BROWSER_HINT)


def _gateway_from_env() -> Gateway:
    """按环境变量连/起一个浏览器并包成 Gateway。"""
    from .gateway import Gateway

    cdp_url = os.environ.get("SLEIGHT_CDP_URL")
    binary = os.environ.get("SLEIGHT_BROWSER")
    if cdp_url:
        from ..core.session import Session
        from ..core.transport import Transport
        from ..providers.plain import Plain
        ep = Plain(cdp_url).endpoint()
        transport = Transport.connect(ep.ws_url, headers=dict(ep.headers))
        session = Session.create(transport)
    elif binary:
        from ..providers.local import LocalLauncher
        fp = os.environ.get("SLEIGHT_FINGERPRINT")
        launcher = LocalLauncher(
            binary, fingerprint=int(fp) if fp else None,
            no_sandbox=os.environ.get("SLEIGHT_NO_SANDBOX") == "1",
        )
        launcher.ensure_ready("default")
        from ..core.session import Session
        from ..core.transport import Transport
        ep = launcher.endpoint("default")
        transport = Transport.connect(ep.ws_url, headers=dict(ep.headers))
        session = Session.create(transport)
    else:
        raise SystemExit(_NO_BROWSER_HINT)
    allow_raw = os.environ.get("SLEIGHT_ALLOW_RAW") == "1"
    return Gateway(session, allow_raw=allow_raw)


def main() -> None:
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    _check_env()                     # 配错就当场退出，别静默起一个永远连不上浏览器的 server
    MCPServer(_gateway_from_env).serve_stdio()


if __name__ == "__main__":
    main()
