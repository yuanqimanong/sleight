"""Official MCP Streamable HTTP transport, hosted by the same Python service."""

import asyncio
import uuid

from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from ..agent.mcp import TOOL_SPECS
from ..core import errors
from .identity import current


def setup_mcp(app, auth, supervisor):
    server = Server("sleight")
    sessions = {}
    lock = asyncio.Lock()

    @server.list_tools()
    async def list_tools():
        return [types.Tool(**spec) for spec in TOOL_SPECS] + [types.Tool(name="browser_release",
            description="End the browser session and reclaim its resources.", inputSchema={"type": "object"})]

    @server.call_tool()
    async def call_tool(name, arguments):
        request = server.request_context.request
        user = auth.resolve(request.headers, request.cookies)
        if not user or "execute" not in user.scopes:
            raise PermissionError("MCP 凭据已失效或没有执行权限")
        context = current.set(user)
        try:
            async with lock:
                with supervisor.db.state("service-sessions") as state:
                    active = {sid for sid, row in state["records"].items() if row["state"] != "released"}
                for key in list(sessions):
                    if sessions[key] not in active:
                        sessions.pop(key, None)
                explicit = request.headers.get("x-sleight-session")
                sid = explicit or sessions.get(user.token_id)
                if name == "browser_release":
                    if sid:
                        await asyncio.to_thread(supervisor.close, sid, user)
                    sessions.pop(user.token_id, None)
                    return [types.TextContent(type="text", text="浏览器会话已结束")]
                if sid:
                    row = await asyncio.to_thread(supervisor.get, sid, user)
                    if row["state"] != "running":
                        if explicit:
                            raise errors.SessionLost("指定的 MCP 浏览器会话已失效")
                        sid = None
                if not sid:
                    if len(sessions) >= 2000:
                        raise errors.Busy("MCP 浏览器会话额度已满，请先释放不再使用的会话")
                    profile = await asyncio.to_thread(supervisor.controller.create, {}, user, "mcp-" + uuid.uuid4().hex)
                    row = await asyncio.to_thread(supervisor.open, {"profile": profile["id"], "options": {"human": True}}, user, uuid.uuid4().hex)
                    sid = sessions[user.token_id] = row["id"]
            message = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}}
            body = {"method": "$mcp", "message": message}
            timeout = arguments.get("timeout", 30)
            if (name == "browser_fetch" and isinstance(timeout, (int, float))
                    and not isinstance(timeout, bool) and .1 <= timeout <= 120):
                body["timeout"] = max(60, timeout + 2)
            reply = await asyncio.to_thread(supervisor.call, sid, user, body)
            result = reply.get("result", {})
            return types.CallToolResult(content=[types.TextContent(**v) for v in result.get("content", [])],
                                        isError=result.get("isError", False))
        finally:
            current.reset(context)

    manager = StreamableHTTPSessionManager(server, stateless=True, json_response=True,
        security_settings=TransportSecuritySettings(enable_dns_rebinding_protection=False))
    app.state.mcp_manager = manager

    class Endpoint:
        async def __call__(self, scope, receive, send):
            request = Request(scope, receive)
            principal = auth.resolve(request.headers, request.cookies)
            if not principal:
                await JSONResponse({"error": "invalid_token"}, status_code=401)(scope, receive, send)
                return
            if "execute" not in principal.scopes:
                await JSONResponse({"error": "execute_scope_required"}, status_code=403)(scope, receive, send)
                return
            try:
                auth.origin(request.headers)
            except Exception:
                await JSONResponse({"error": "origin_refused"}, status_code=403)(scope, receive, send)
                return
            await manager.handle_request(scope, receive, send)

    app.router.routes.append(Route("/mcp", Endpoint(), methods=["GET", "POST", "DELETE"]))
