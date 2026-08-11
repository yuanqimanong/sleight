"""真浏览器下的 MCP server：走 JSON-RPC 把 open→snapshot→find→click 全链路跑通。

不需要外部 MCP client——直接喂 handle() 一条条 JSON-RPC 消息，验证端到端行为与真实点击。
无浏览器时 skip。
"""

from __future__ import annotations

import json

import pytest

from sleight.agent import Gateway, MCPServer

from .conftest import serve_pages

pytestmark = pytest.mark.integration

_PAGE = (
    "<!doctype html><title>T</title><h1>Head</h1>"
    "<button id=go onclick='window.__hit = event.isTrusted'>Submit order</button>"
)


def _call(server, name, args):
    return server.handle({"jsonrpc": "2.0", "id": 9, "method": "tools/call",
                          "params": {"name": name, "arguments": args}})["result"]


def test_mcp_drives_a_real_browser_end_to_end(live_session):
    server = MCPServer(lambda: Gateway(live_session))

    # 握手
    init = server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                          "params": {"protocolVersion": "2024-11-05"}})
    assert init["result"]["serverInfo"]["name"] == "sleight"
    tools = server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]
    assert len(tools) == 4

    with serve_pages({"p.html": _PAGE}) as d:
        assert not _call(server, "browser_session",
                         {"action": "open", "url": f"file://{d}/p.html", "wait_text": "Head"})["isError"]

        snap = _call(server, "browser_observe", {"action": "snapshot"})
        assert not snap["isError"] and "button" in snap["content"][0]["text"]

        found = _call(server, "browser_observe", {"action": "find", "query": "submit"})
        ref = json.loads(found["content"][0]["text"])["matches"][0]["ref"]

        clicked = _call(server, "browser_act", {"action": "click", "ref": ref})
        assert not clicked["isError"]
        assert live_session.eval("window.__hit") is True, "按 MCP 工具点击应产生可信事件"
