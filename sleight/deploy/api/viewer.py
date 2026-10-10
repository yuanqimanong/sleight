"""Official Manager dashboard/VNC behind the Sleight authentication boundary."""

import asyncio
import json
import re
from contextlib import ExitStack

import httpx
import websockets
from fastapi import HTTPException, Request, WebSocket
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from starlette.background import BackgroundTask

HOP = {"connection", "keep-alive", "transfer-encoding", "upgrade", "content-length"}


def shim(prefix):
    prefix = json.dumps(prefix)
    return f"""<script>(()=>{{const base={prefix};
const rewrite=(v)=>{{const u=new URL(v,location.href);if(u.origin===location.origin||u.host===location.host){{
if(!u.pathname.startsWith(base))u.pathname=base+u.pathname;u.searchParams.delete('token');}}return u.href}};
const fetch0=window.fetch;window.fetch=(v,o)=>fetch0(v instanceof Request?new Request(rewrite(v.url),v):rewrite(v),o);
const WS=window.WebSocket;
function SleightSocket(v,p){{return arguments.length>1?new WS(rewrite(v),p):new WS(rewrite(v))}}
SleightSocket.prototype=WS.prototype;Object.setPrototypeOf(SleightSocket,WS);window.WebSocket=SleightSocket;
}})();</script>"""


def setup_viewer(app, resolve):
    @app.api_route("/viewer/{host}/{deployment}/{path:path}", methods=["GET", "HEAD", "POST"])
    async def viewer(request: Request, host: str, deployment: str, path: str):
        if any(p in (".", "..") for p in path.split("/")):
            raise HTTPException(400, "Invalid viewer path")
        dep, _ = resolve(f"{host}/{deployment}")
        stack = ExitStack()
        manager = await asyncio.to_thread(stack.enter_context, dep.connect())
        client = httpx.AsyncClient(timeout=httpx.Timeout(30, read=600), trust_env=False)
        try:
            if request.method == "POST":
                match = re.fullmatch(r"api/profiles/([^/]+)/(launch|stop)", path)
                if not match:
                    raise HTTPException(403, "请在 Sleight 中修改实例配置")
                pid, action = match.groups()
                await asyncio.to_thread(manager.ensure_ready if action == "launch" else manager.stop, pid)
                return JSONResponse({"id": pid, "status": "running" if action == "launch" else "stopped"})
            url = str(httpx.URL(manager.base_url + "/" + path).copy_with(query=request.scope.get("query_string", b"")))
            response = await client.send(client.build_request(request.method, url,
                                         headers={"Authorization": "Bearer " + manager.token}), stream=True)
            content_type = response.headers.get("content-type", "")
            prefix = request.scope.get("root_path", "") + f"/viewer/{host}/{deployment}"
            if "text/html" in content_type:
                body = (await response.aread()).decode()
                body = re.sub(r'((?:src|href)=["\'])/(?!/)', lambda m: m[1] + prefix + "/", body)
                body = body.replace("<head>", "<head>" + shim(prefix), 1)
                await response.aclose()
                await client.aclose()
                stack.close()
                return HTMLResponse(body, headers={"Cache-Control": "no-store"})
            headers = {k: v for k, v in response.headers.items() if k not in HOP | {"set-cookie"}}

            async def finish():
                await response.aclose()
                await client.aclose()
                await asyncio.to_thread(stack.close)

            return StreamingResponse(response.aiter_raw(), status_code=response.status_code,
                                     headers=headers, background=BackgroundTask(finish))
        except BaseException:
            await client.aclose()
            await asyncio.to_thread(stack.close)
            raise
        finally:
            if request.method == "POST":
                await client.aclose()
                await asyncio.to_thread(stack.close)

    @app.websocket("/viewer/{host}/{deployment}/{path:path}")
    async def viewer_socket(socket: WebSocket, host: str, deployment: str, path: str):
        # Restrict this tunnel to the official viewer; CDP is for SDK clients.
        if not re.fullmatch(r"api/profiles/[^/]+/vnc", path):
            await socket.close(code=4403)
            return
        dep, _ = resolve(f"{host}/{deployment}")
        with ExitStack() as stack:
            manager = await asyncio.to_thread(stack.enter_context, dep.connect())
            url = (manager.base_url + "/" + path).replace("http", "ws", 1)
            async with websockets.connect(url, additional_headers={"Authorization": "Bearer " + manager.token},
                                          subprotocols=socket.scope.get("subprotocols") or None, max_size=16*1024*1024) as remote:
                await socket.accept(subprotocol=remote.subprotocol)

                async def upload():
                    while True:
                        item = await socket.receive()
                        if item["type"] == "websocket.disconnect":
                            return
                        await remote.send(item.get("bytes") if item.get("bytes") is not None else item["text"])

                async def download():
                    async for item in remote:
                        await socket.send_bytes(item) if isinstance(item, bytes) else await socket.send_text(item)

                async def authorization():
                    while app.state.auth.valid(socket.headers, socket.cookies):
                        await asyncio.sleep(5)
                    await socket.close(code=4401)

                tasks = [asyncio.create_task(upload()), asyncio.create_task(download()), asyncio.create_task(authorization())]
                try:
                    await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
