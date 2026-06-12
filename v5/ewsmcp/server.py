"""MCP wiring: low-level Server, annotations, structured output, lifecycle."""

import logging
from typing import Any, Dict, List

from mcp.server import Server
from mcp.types import Tool, ToolAnnotations

from .audit import AuditLog
from .config import Settings
from .gateway.client import EWSGateway
from .gateway.connection import ConnectionManager
from .ids import get_aliaser
from .tools import build_registry
from .tools.base import Context, dispatch

logger = logging.getLogger(__name__)

ANNOTATIONS = {
    "read": ToolAnnotations(readOnlyHint=True, destructiveHint=False,
                            idempotentHint=True, openWorldHint=False),
    "write": ToolAnnotations(readOnlyHint=False, destructiveHint=False,
                             idempotentHint=False, openWorldHint=False),
    "destructive": ToolAnnotations(readOnlyHint=False, destructiveHint=True,
                                   idempotentHint=False, openWorldHint=False),
    "send": ToolAnnotations(readOnlyHint=False, destructiveHint=True,
                            idempotentHint=False, openWorldHint=True),
}


def build_context(settings: Settings) -> Context:
    gateway = EWSGateway(settings)
    ctx = Context(
        settings=settings,
        gateway=gateway,
        manager=None,
        aliaser=get_aliaser(f"{settings.data_dir}/memory"),
        audit=AuditLog(settings.data_dir),
    )
    build_registry(ctx)
    return ctx


async def start_connection_manager(ctx: Context) -> None:
    manager = ConnectionManager(
        ctx.gateway,
        max_backoff=float(ctx.settings.ews_warmup_max_backoff_seconds),
        heartbeat_seconds=int(ctx.settings.ews_heartbeat_seconds),
    )
    ctx.manager = manager
    await manager.start()
    logger.info("Exchange warmup running in background (see /readyz)")


def build_mcp_server(ctx: Context) -> Server:
    server = Server("ews-mcp-v5")

    @server.list_tools()
    async def list_tools() -> List[Tool]:
        tools = []
        for spec in ctx.registry.values():
            schema = spec.public_schema()
            tools.append(Tool(
                name=schema["name"],
                description=schema["description"],
                inputSchema=schema["inputSchema"],
                annotations=ANNOTATIONS.get(spec.side_effect_class, ANNOTATIONS["write"]),
            ))
        return tools

    @server.call_tool()
    async def call_tool(name: str, arguments: Dict[str, Any]):
        spec = ctx.registry.get(name)
        if spec is None:
            return {"ok": False, "error": {
                "code": "validation",
                "message": f"Unknown tool: {name}",
                "hint": f"Available: {', '.join(sorted(ctx.registry))}",
            }}
        return await dispatch(ctx, spec, dict(arguments or {}), transport="mcp")

    return server


async def run_stdio(settings: Settings) -> None:
    from mcp.server.stdio import stdio_server

    ctx = build_context(settings)
    server = build_mcp_server(ctx)
    await start_connection_manager(ctx)
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())

