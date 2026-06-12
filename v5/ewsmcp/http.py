"""HTTP serving: Streamable HTTP /mcp + REST shim + health (IDEATION §4)."""

import hmac
import json
import logging
from typing import Any, Dict

from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

from . import __version__
from .errors import HTTP_BY_CODE
from .server import build_context, build_mcp_server, start_connection_manager, status_payload
from .tools.base import dispatch

logger = logging.getLogger(__name__)

PUBLIC_PATHS = {"/health", "/livez", "/readyz", "/version"}


def _authorized(headers, api_key: str) -> bool:
    expected = api_key.encode()
    for name, value in headers or []:
        lname = name.lower() if isinstance(name, bytes) else str(name).encode().lower()
        raw = value.decode("utf-8", "replace") if isinstance(value, bytes) else str(value)
        if lname == b"authorization" and raw.lower().startswith("bearer "):
            if hmac.compare_digest(raw[7:].strip().encode(), expected):
                return True
        elif lname == b"x-api-key":
            if hmac.compare_digest(raw.strip().encode(), expected):
                return True
    return False


async def _send_json(send, status: int, payload: Dict[str, Any]) -> None:
    body = json.dumps(payload, ensure_ascii=False, default=str).encode()
    await send({"type": "http.response.start", "status": status, "headers": [
        [b"content-type", b"application/json"],
        [b"content-length", str(len(body)).encode()],
    ]})
    await send({"type": "http.response.body", "body": body})


def _openapi(ctx) -> Dict[str, Any]:
    paths = {}
    for name, spec in ctx.registry.items():
        schema = spec.public_schema()
        paths[f"/api/tools/{name}"] = {"post": {
            "operationId": name,
            "summary": schema["description"][:120],
            "requestBody": {"content": {"application/json": {"schema": schema["inputSchema"]}}},
            "responses": {"200": {"description": "tool result"}},
        }}
    return {"openapi": "3.0.3",
            "info": {"title": "ews-mcp v5", "version": __version__},
            "paths": paths}


async def serve_http(settings) -> None:
    import uvicorn

    ctx = build_context(settings)
    mcp_server = build_mcp_server(ctx)
    streamable = StreamableHTTPSessionManager(app=mcp_server, json_response=False,
                                              stateless=True)
    api_key = settings.mcp_api_key or ""

    async def app(scope, receive, send):
        if scope["type"] == "lifespan":
            while True:
                message = await receive()
                if message["type"] == "lifespan.startup":
                    await start_connection_manager(ctx)
                    await send({"type": "lifespan.startup.complete"})
                elif message["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        if scope["type"] != "http":
            return
        path, method = scope["path"], scope["method"]

        if path == "/livez" and method == "GET":
            return await _send_json(send, 200, {"status": "ok"})
        if path == "/health" and method == "GET":
            return await _send_json(send, 200, {"status": "ok", "tools": len(ctx.registry)})
        if path == "/version" and method == "GET":
            return await _send_json(send, 200, {"version": __version__})
        if path == "/readyz" and method == "GET":
            conn = ctx.manager.status() if ctx.manager else {"state": "unmanaged"}
            warm = conn.get("state") in ("warm", "unmanaged")
            return await _send_json(send, 200 if warm else 503, {
                "status": "ok" if warm else "unavailable",
                "connection": conn, "tools": len(ctx.registry),
            })

        if api_key and not _authorized(scope.get("headers"), api_key):
            return await _send_json(send, 401, {"ok": False, "error": {
                "code": "auth_failed", "message": "missing or invalid bearer token"}})

        if path == "/mcp":
            return await streamable.handle_request(scope, receive, send)
        if path == "/openapi.json" and method == "GET":
            return await _send_json(send, 200, _openapi(ctx))
        if path == "/api/tools" and method == "GET":
            return await _send_json(send, 200, {"tools": [
                {"name": s.name, "class": s.side_effect_class,
                 "description": s.description[:140]}
                for s in ctx.registry.values()
            ]})
        if path.startswith("/api/tools/") and method == "POST":
            name = path.removeprefix("/api/tools/")
            spec = ctx.registry.get(name)
            if spec is None:
                return await _send_json(send, 404, {"ok": False, "error": {
                    "code": "validation", "message": f"Unknown tool: {name}"}})
            chunks = []
            while True:
                message = await receive()
                if message["type"] == "http.request":
                    chunks.append(message.get("body", b""))
                    if not message.get("more_body"):
                        break
            try:
                arguments = json.loads(b"".join(chunks) or b"{}")
            except Exception as e:
                return await _send_json(send, 400, {"ok": False, "error": {
                    "code": "validation", "message": f"invalid JSON body: {e}"}})
            result = await dispatch(ctx, spec, arguments, transport="rest")
            status = 200
            if isinstance(result, dict) and result.get("ok") is False:
                status = HTTP_BY_CODE.get(result.get("error", {}).get("code", ""), 500)
            return await _send_json(send, status, result)

        return await _send_json(send, 404, {"ok": False, "error": {
            "code": "validation", "message": "not found"}})

    config = uvicorn.Config(app, host=settings.mcp_host, port=settings.mcp_port,
                            log_level=settings.log_level.lower(), http="h11")
    async with streamable.run():
        await uvicorn.Server(config).serve()
