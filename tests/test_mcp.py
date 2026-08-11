"""MCP server 协议处理：纯 JSON-RPC 单测，不开浏览器。

用一个假 Gateway 记录路由，验证 initialize / tools/list / tools/call 分发、结果格式化、
通知不回、未知方法报 JSON-RPC error、stdio 解析错误、Gateway 懒创建。
"""

from __future__ import annotations

import io

from sleight.agent import MCPServer
from sleight.agent.gateway import ToolResult


class _FakeGateway:
    def __init__(self) -> None:
        self.calls: list = []

    def session(self, action, **kw):
        self.calls.append(("session", action, kw))
        return ToolResult(True, "session", action, {"url": "u", "title": "t"})

    def observe(self, action="snapshot", **kw):
        self.calls.append(("observe", action, kw))
        data = ({"snapshot": "TREE", "refs": ["e1"]} if action == "snapshot"
                else {"matches": [{"ref": "e1", "role": "button", "name": "Go"}]})
        return ToolResult(True, "observe", action, data)

    def act(self, action, **kw):
        self.calls.append(("act", action, kw))
        if kw.get("ref") == "bad":
            return ToolResult.fail("act", action, "unknown ref 'bad'")
        return ToolResult(True, "act", action, {"ref": kw.get("ref"), "coordinate": kw.get("coordinate")})

    def extract(self, **kw):
        self.calls.append(("extract", kw))
        return ToolResult(True, "extract", "document", {"title": "T", "quality": 0.5})


def _server():
    gw = _FakeGateway()
    return MCPServer(lambda: gw), gw


def _call(server, name, args):
    return server.handle({"jsonrpc": "2.0", "id": 9, "method": "tools/call",
                          "params": {"name": name, "arguments": args}})["result"]


def test_initialize_echoes_protocol_and_reports_server_info():
    server, _ = _server()
    r = server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                       "params": {"protocolVersion": "2024-11-05"}})
    assert r["result"]["protocolVersion"] == "2024-11-05"
    assert r["result"]["serverInfo"]["name"] == "sleight"
    assert "tools" in r["result"]["capabilities"]


def test_notifications_get_no_response():
    server, _ = _server()
    assert server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


def test_tools_list_exposes_the_four_gateway_tools():
    server, _ = _server()
    tools = server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]
    assert {t["name"] for t in tools} == {
        "browser_session", "browser_observe", "browser_act", "browser_extract"
    }
    for t in tools:
        assert t["description"] and t["inputSchema"]["type"] == "object"


def test_tools_call_routes_to_gateway_and_formats_results():
    server, gw = _server()
    # snapshot -> 文本内容（不 JSON 转义）
    r = _call(server, "browser_observe", {"action": "snapshot"})
    assert r["isError"] is False and "TREE" in r["content"][0]["text"]
    assert "e1" in r["content"][0]["text"]
    # session open 透传 url/wait
    _call(server, "browser_session", {"action": "open", "url": "x", "wait_text": "Hi"})
    assert ("session", "open", {"url": "x", "wait_text": "Hi", "wait_selector": None,
                                "timeout": 30.0}) in gw.calls
    # act click via ref
    _call(server, "browser_act", {"action": "click", "ref": "e1"})
    assert any(c[0] == "act" and c[2].get("ref") == "e1" for c in gw.calls)


def test_act_coordinate_is_passed_as_a_tuple():
    server, gw = _server()
    _call(server, "browser_act", {"action": "click", "coordinate": [10, 20]})
    act = next(c for c in gw.calls if c[0] == "act")
    assert act[2]["coordinate"] == (10, 20)


def test_gateway_tool_failure_is_reported_as_iserror():
    server, _ = _server()
    r = _call(server, "browser_act", {"action": "click", "ref": "bad"})
    assert r["isError"] is True and "unknown ref" in r["content"][0]["text"]


def test_unknown_tool_is_iserror_not_a_protocol_error():
    server, _ = _server()
    r = _call(server, "browser_teleport", {})
    assert r["isError"] is True


def test_unknown_method_is_a_jsonrpc_error():
    server, _ = _server()
    e = server.handle({"jsonrpc": "2.0", "id": 5, "method": "bogus"})
    assert e["error"]["code"] == -32601


def test_gateway_is_created_lazily():
    created = []

    def provider():
        created.append(1)
        return _FakeGateway()

    server = MCPServer(provider)
    server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    assert not created, "initialize / tools/list 不该连浏览器"
    _call(server, "browser_extract", {})
    assert created == [1], "第一个 tools/call 才创建 Gateway"


def test_serve_stdio_reads_lines_and_reports_parse_errors():
    server, _ = _server()
    inp = io.StringIO('not json\n{"jsonrpc":"2.0","id":1,"method":"ping"}\n')
    out = io.StringIO()
    server.serve_stdio(inp, out)
    lines = [line for line in out.getvalue().splitlines() if line]
    import json
    parsed = [json.loads(x) for x in lines]
    assert parsed[0]["error"]["code"] == -32700          # 坏 JSON
    assert parsed[1]["result"] == {}                     # ping


def test_env_check_fails_fast_when_no_browser_is_configured(monkeypatch):
    """配错环境变量必须启动即失败。Gateway 是懒创建的，等到第一次 tools/call 才报，
    在 MCP host 里就表现成"起来了、什么都不说、然后静默退出"，日志里毫无线索。"""
    import pytest

    from sleight.agent.mcp import _check_env

    monkeypatch.delenv("SLEIGHT_CDP_URL", raising=False)
    monkeypatch.delenv("SLEIGHT_BROWSER", raising=False)
    with pytest.raises(SystemExit, match="SLEIGHT_CDP_URL"):
        _check_env()

    monkeypatch.setenv("SLEIGHT_CDP_URL", "http://127.0.0.1:9222")
    _check_env()                       # 配了就不抛
