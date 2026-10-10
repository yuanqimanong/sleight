"""Attach a Sleight execution host without exposing its token to the browser."""

import asyncio
import uuid
from urllib.parse import urlsplit

import httpx
import websockets
from fastapi import Body, HTTPException, Request, WebSocket
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask

from ...runtime.governor import SQLiteLedger
from ...service.database import database
from ...service.identity import Vault

HOP = {"connection", "keep-alive", "transfer-encoding", "upgrade", "content-length", "set-cookie"}


def setup_agents(app):
    ledger = SQLiteLedger()
    vault = Vault()
    vault.import_file("agent-secrets.json", "agent:")
    db = database()
    service_id = db.get("service", "id")
    if not service_id:
        service_id = uuid.uuid4().hex
        db.put("service", "id", service_id)
    delegation_lock = asyncio.Lock()

    async def delegated(connection, row, secret):
        principal = app.state.auth.resolve(connection.headers, connection.cookies)
        if not principal:
            raise HTTPException(401, "控制台凭据已失效")
        key = row["id"] + ":" + principal.user_id
        async with delegation_lock:
            cached = db.get("agent-delegations", key, {})
            if cached.get("expires", 0) > db.now() + 30:
                return vault.cipher.decrypt(cached["token"].encode()).decode()
            async with httpx.AsyncClient(timeout=15, trust_env=False) as client:
                response = await client.post(row["url"] + "/api/v1/delegate", headers={"X-Sleight-Token": secret},
                    json={"external_id": "sleight:" + service_id + ":" + principal.user_id,
                          "name": "Sleight 执行端 / " + principal.user_id, "ttl": 900})
                if not response.is_success:
                    raise HTTPException(502, "执行端接入口令需要 delegate 权限及配套服务版本")
                value = response.json()
            db.put("agent-delegations", key, {"expires": value["expires"],
                   "token": vault.cipher.encrypt(value["token"].encode()).decode()})
            return value["token"]

    def lookup(pid):
        with ledger.transaction("agents") as state:
            row = state["records"].get(pid)
            if not row:
                raise HTTPException(404, "执行端不存在")
        secret = vault.get("agent:" + pid)
        return row, secret

    @app.get("/api/agents")
    def list_agents():
        with ledger.transaction("agents") as state:
            return list(state["records"].values())

    @app.post("/api/agents")
    async def add_agent(body: dict = Body(...)):
        url, token = str(body.get("url", "")).rstrip("/"), str(body.get("token", ""))
        parsed = urlsplit(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.query or parsed.fragment:
            raise HTTPException(400, "执行端地址需为 http(s) URL，凭据请单独填写")
        if not token:
            raise HTTPException(400, "请填写执行端签发的 delegate 接入 token")
        async with httpx.AsyncClient(timeout=15, trust_env=False) as client:
            try:
                response = await client.get(url + "/api/auth/me", headers={"X-Sleight-Token": token})
                response.raise_for_status()
                identity = response.json()
                if "delegate" not in identity.get("scopes", []) or identity.get("user", {}).get("role") != "admin":
                    raise ValueError("delegate required")
                platform = identity["platform"]
            except (httpx.HTTPError, ValueError):
                raise HTTPException(400, "执行端无法连接，请检查地址、delegate 接入 token 和服务版本") from None
        pid = uuid.uuid4().hex
        row = {"id": pid, "name": str(body.get("name") or platform["os"]), "url": url,
               "os": platform["os"], "arch": platform["arch"]}
        vault.put("agent:" + pid, token)
        with ledger.transaction("agents") as state:
            state["records"][pid] = row
        return row

    @app.delete("/api/agents/{pid}")
    def remove_agent(pid: str):
        with ledger.transaction("agents") as state:
            if pid not in state["records"]:
                raise HTTPException(404, "执行端不存在")
            del state["records"][pid]
        vault.delete("agent:" + pid)
        for user in app.state.auth.identity.users():
            db.delete("agent-delegations", pid + ":" + user["id"])
        return {"ok": True}

    @app.api_route("/agent/{pid}/{path:path}", methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"])
    async def agent_proxy(request: Request, pid: str, path: str):
        row, secret = lookup(pid)
        if path.startswith("api/agents") or path.startswith("agent/") or (path.startswith("api/auth/") and path != "api/auth/me"):
            raise HTTPException(403, "执行端连接与登录由主控制台管理")
        if any(p in (".", "..") for p in path.split("/")):
            raise HTTPException(400, "非法路径")
        secret = await delegated(request, row, secret)
        url = str(httpx.URL(row["url"] + "/" + path).copy_with(query=request.scope.get("query_string", b"")))
        prefix = request.scope.get("root_path", "") + "/agent/" + pid
        client = httpx.AsyncClient(timeout=httpx.Timeout(30, read=600), trust_env=False, follow_redirects=False)
        headers = {"X-Sleight-Token": secret, "X-Forwarded-Prefix": prefix,
                   "Host": request.headers["host"], "Content-Type": request.headers.get("content-type", "")}
        for name in ("accept", "idempotency-key", "mcp-protocol-version", "x-sleight-session"):
            if name in request.headers:
                headers[name] = request.headers[name]
        try:
            response = await client.send(client.build_request(request.method, url, headers=headers,
                                         content=request.stream()), stream=True)
        except httpx.HTTPError:
            await client.aclose()
            raise HTTPException(502, "执行端暂时不可达") from None
        headers = {k: v for k, v in response.headers.items() if k not in HOP | {"location"}}
        location = response.headers.get("location")
        if location:
            if location.startswith(row["url"] + "/"):
                location = prefix + location[len(row["url"]):]
            elif location.startswith("/"):
                location = prefix + location
            elif urlsplit(location).netloc:
                location = prefix + "/"
            headers["Location"] = location
        headers["X-Accel-Buffering"] = "no"
        headers["Cache-Control"] = "no-store"

        async def finish():
            await response.aclose()
            await client.aclose()

        return StreamingResponse(response.aiter_raw(), status_code=response.status_code,
                                 headers=headers, background=BackgroundTask(finish))

    @app.websocket("/agent/{pid}/{path:path}")
    async def agent_socket(socket: WebSocket, pid: str, path: str):
        if not path.startswith(("viewer/", "api/runtime/profiles/", "api/v1/sessions/")) or any(p in (".", "..") for p in path.split("/")):
            await socket.close(code=4403)
            return
        row, secret = lookup(pid)
        secret = await delegated(socket, row, secret)
        url = str(httpx.URL(row["url"] + "/" + path).copy_with(query=socket.scope.get("query_string", b""))).replace("http", "ws", 1)
        async with websockets.connect(url, additional_headers={"X-Sleight-Token": secret},
                                      subprotocols=socket.scope.get("subprotocols") or None) as remote:
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
