"""Thin official stdio MCP client: all operations and ownership stay in Sleight."""

import asyncio
import os
from contextlib import suppress

import httpx
from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server


def main():
    if not os.environ.get("SLEIGHT_TOKEN"):
        raise SystemExit("SLEIGHT_TOKEN is required with SLEIGHT_UPSTREAM")
    asyncio.run(serve())


async def serve():
    from ..client import ServiceClient
    ServiceClient(os.environ["SLEIGHT_UPSTREAM"], os.environ["SLEIGHT_TOKEN"]).close()
    server = Server("sleight-client")
    async with httpx.AsyncClient(base_url=os.environ["SLEIGHT_UPSTREAM"].rstrip("/") + "/", timeout=300, trust_env=False,
            headers={"X-Sleight-Token": os.environ["SLEIGHT_TOKEN"], "Accept": "application/json, text/event-stream"}) as client:
        async def request(method, params=None):
            reply = await client.post("mcp", json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}})
            reply.raise_for_status()
            value = reply.json()
            if "error" in value:
                raise ValueError(value["error"].get("message", "MCP request failed"))
            return value.get("result", {})

        @server.list_tools()
        async def list_tools():
            value = await request("tools/list")
            return [types.Tool(**r) for r in value["tools"]]

        @server.call_tool()
        async def call_tool(name, arguments):
            value = await request("tools/call", {"name": name, "arguments": arguments})
            return types.CallToolResult(**value)

        try:
            async with stdio_server() as streams:
                await server.run(*streams, server.create_initialization_options())
        finally:
            with suppress(httpx.HTTPError):
                await request("tools/call", {"name": "browser_release", "arguments": {}})
