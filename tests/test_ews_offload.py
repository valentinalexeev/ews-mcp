"""R0 reliability regression tests: EWS work off the event loop.

exchangelib is synchronous; tool bodies used to run inline in async
handlers, freezing the whole event loop (every concurrent request, SSE
keepalives, /health) for the duration of each Exchange round-trip.
``BaseTool._run_execute`` now offloads EWS-touching tool bodies to a
bounded worker pool. These tests prove:

  * the event loop keeps servicing other tasks while a tool blocks;
  * EWS_OFFLOAD_ENABLED=false restores the historical inline path;
  * tools whose bodies use ``await asyncio.to_thread(...)`` internally
    still work when offloaded (nested-loop compatibility);
  * exceptions raised on the worker propagate into the normal
    safe_execute error handling.

Coroutines are driven with ``asyncio.run`` so the suite does not depend on
pytest-asyncio configuration.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Dict

from src.config import Settings
from src.tools.base import BaseTool


def _settings(**overrides) -> Settings:
    base = dict(
        ews_email="exec@corp.example",
        ews_auth_type="basic",
        ews_username="DOMAIN\\svc",
        ews_password="pw",
        ews_autodiscover=False,
        ews_server_url="https://mail.corp.example/EWS/Exchange.asmx",
        rate_limit_enabled=False,
        enable_audit_log=False,
    )
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


class _FakeClient:
    def __init__(self, config):
        self.config = config


class _BlockingTool(BaseTool):
    """Simulates a slow exchangelib round-trip with a hard blocking sleep."""

    side_effect_class = "read"

    def get_schema(self) -> Dict[str, Any]:
        return {
            "name": "dummy_blocking",
            "description": "test-only blocking tool",
            "inputSchema": {"type": "object", "properties": {}},
        }

    async def execute(self, **kwargs) -> Dict[str, Any]:
        time.sleep(0.5)  # blocking on purpose — stands in for EWS I/O
        return {"success": True, "message": "done", "executed": True}


class _NestedToThreadTool(BaseTool):
    """Body that itself uses asyncio.to_thread (person_service pattern)."""

    side_effect_class = "read"

    def get_schema(self) -> Dict[str, Any]:
        return {
            "name": "dummy_nested",
            "description": "test-only nested to_thread tool",
            "inputSchema": {"type": "object", "properties": {}},
        }

    async def execute(self, **kwargs) -> Dict[str, Any]:
        value = await asyncio.to_thread(lambda: 41 + 1)
        return {"success": True, "message": "done", "value": value}


class _RaisingTool(BaseTool):
    side_effect_class = "read"

    def get_schema(self) -> Dict[str, Any]:
        return {
            "name": "dummy_raising",
            "description": "test-only raising tool",
            "inputSchema": {"type": "object", "properties": {}},
        }

    async def execute(self, **kwargs) -> Dict[str, Any]:
        raise ValueError("worker-side boom")


async def _count_ticks_while(tool_coro, tick_seconds: float = 0.05) -> int:
    """Run a tool call while ticking on the event loop; return tick count."""
    ticks = 0
    done = asyncio.Event()

    async def _ticker():
        nonlocal ticks
        while not done.is_set():
            ticks += 1
            await asyncio.sleep(tick_seconds)

    ticker = asyncio.create_task(_ticker())
    try:
        await tool_coro
    finally:
        done.set()
        await ticker
    return ticks


def test_offload_keeps_event_loop_responsive():
    tool = _BlockingTool(_FakeClient(_settings(ews_offload_enabled=True)))

    async def scenario():
        return await _count_ticks_while(tool.safe_execute())

    ticks = asyncio.run(scenario())
    # 0.5s blocking body, 50ms ticks: a responsive loop sees ~10 ticks.
    assert ticks >= 5, f"event loop starved during offloaded call ({ticks} ticks)"


def test_inline_path_blocks_event_loop():
    # Documents the rollback switch's behavior: inline = historical freeze.
    tool = _BlockingTool(_FakeClient(_settings(ews_offload_enabled=False)))

    async def scenario():
        return await _count_ticks_while(tool.safe_execute())

    ticks = asyncio.run(scenario())
    assert ticks <= 3, f"expected inline path to starve the loop ({ticks} ticks)"


def test_offloaded_result_and_success_path_intact():
    tool = _BlockingTool(_FakeClient(_settings(ews_offload_enabled=True)))
    result = asyncio.run(tool.safe_execute())
    assert result.get("executed") is True


def test_nested_to_thread_works_when_offloaded():
    tool = _NestedToThreadTool(_FakeClient(_settings(ews_offload_enabled=True)))
    result = asyncio.run(tool.safe_execute())
    assert result.get("value") == 42


def test_worker_exception_propagates_to_error_response():
    tool = _RaisingTool(_FakeClient(_settings(ews_offload_enabled=True)))
    result = asyncio.run(tool.safe_execute())
    assert result["success"] is False
    assert "worker-side boom" in result["error"]
