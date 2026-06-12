"""R0 reliability regression tests: never-exit connection lifecycle.

Covers:
  * ConnectionManager warmup loop: retries with backoff, recovery ladder
    (session reset every Nth failure), state transitions, on_warm fires once.
  * status() snapshot shape consumed by /readyz and whoami.
  * BaseTool cold-start gate: EWS-touching tools fail fast with a retry
    hint while state == "connecting"; requires_ews=False tools pass.

Coroutines are driven with ``asyncio.run`` so the suite does not depend on
pytest-asyncio configuration.
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict

from src.config import Settings
from src.connection_manager import (
    STATE_CONNECTING,
    STATE_WARM,
    ConnectionManager,
)
from src.tools.base import BaseTool


class _FlakyClient:
    """EWSClient stand-in whose test_connection fails N times then succeeds."""

    def __init__(self, fail_times: int):
        self.fail_times = fail_times
        self.calls = 0
        self.resets = 0
        self._account = None

    def test_connection(self) -> bool:
        self.calls += 1
        if self.calls <= self.fail_times:
            raise ConnectionError(f"probe failed (attempt {self.calls})")
        return True

    def reset(self) -> None:
        self.resets += 1


def _manager(client, **kwargs) -> ConnectionManager:
    defaults = dict(initial_backoff=0.01, max_backoff=0.05, heartbeat_seconds=0)
    defaults.update(kwargs)
    return ConnectionManager(client, **defaults)


# --- warmup loop -------------------------------------------------------------


def test_warmup_eventually_warm_after_failures():
    async def scenario():
        client = _FlakyClient(fail_times=4)
        manager = _manager(client)
        fired = []

        async def on_warm():
            fired.append(True)

        await manager.start(on_warm=on_warm)
        assert manager.state == STATE_CONNECTING
        for _ in range(200):  # generous budget; backoff is tiny
            if manager.is_warm:
                break
            await asyncio.sleep(0.02)
        assert manager.is_warm
        # 4 failures then success; recovery ladder reset at failure #3.
        assert client.calls == 5
        assert client.resets >= 1
        assert fired == [True]
        await manager.stop()

    asyncio.run(scenario())


def test_warmup_first_try_success_no_resets():
    async def scenario():
        client = _FlakyClient(fail_times=0)
        manager = _manager(client)
        await manager.start()
        for _ in range(100):
            if manager.is_warm:
                break
            await asyncio.sleep(0.01)
        assert manager.is_warm
        assert client.resets == 0
        status = manager.status()
        assert status["state"] == STATE_WARM
        assert status["last_error"] is None
        assert status["next_retry_in_s"] is None
        assert status["last_success_age_s"] is not None
        await manager.stop()

    asyncio.run(scenario())


def test_status_while_connecting_reports_error_and_retry():
    async def scenario():
        client = _FlakyClient(fail_times=10_000)
        manager = _manager(client, initial_backoff=5.0, max_backoff=5.0)
        await manager.start()
        for _ in range(100):
            if manager.status()["attempts"] >= 1:
                break
            await asyncio.sleep(0.01)
        status = manager.status()
        assert status["state"] == STATE_CONNECTING
        assert status["attempts"] >= 1
        assert "probe failed" in (status["last_error"] or "")
        await manager.stop()

    asyncio.run(scenario())


# --- BaseTool cold-start gate -------------------------------------------------


class _FakeManager:
    def __init__(self, state: str):
        self.state = state

    def status(self) -> Dict[str, Any]:
        return {
            "state": self.state,
            "attempts": 7,
            "last_error": "TransportError: Failed to get auth type from service",
            "last_success_age_s": None,
            "next_retry_in_s": 42,
        }


class _FakeClient:
    def __init__(self, config, manager=None):
        self.config = config
        if manager is not None:
            self.connection_manager = manager


class _ReadTool(BaseTool):
    side_effect_class = "read"

    def get_schema(self) -> Dict[str, Any]:
        return {
            "name": "dummy_read",
            "description": "test-only read tool",
            "inputSchema": {"type": "object", "properties": {}},
        }

    async def execute(self, **kwargs) -> Dict[str, Any]:
        return {"success": True, "message": "executed", "executed": True}


class _LocalTool(_ReadTool):
    requires_ews = False

    def get_schema(self) -> Dict[str, Any]:
        schema = super().get_schema()
        schema["name"] = "dummy_local"
        return schema


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


def test_cold_gate_blocks_ews_tool_with_retry_hint():
    tool = _ReadTool(_FakeClient(_settings(), _FakeManager("connecting")))
    result = asyncio.run(tool.safe_execute())
    assert result["success"] is False
    assert "warming up" in result["error"]
    assert "42" in result["error"]  # retry hint surfaced
    assert "auth type" in result["error"]  # last error surfaced


def test_cold_gate_passes_local_tool():
    tool = _LocalTool(_FakeClient(_settings(), _FakeManager("connecting")))
    result = asyncio.run(tool.safe_execute())
    assert result.get("executed") is True


def test_warm_manager_does_not_gate():
    tool = _ReadTool(_FakeClient(_settings(), _FakeManager("warm")))
    result = asyncio.run(tool.safe_execute())
    assert result.get("executed") is True


def test_absent_manager_does_not_gate():
    # Tests and stdio tools constructed without a manager behave as before.
    tool = _ReadTool(_FakeClient(_settings()))
    result = asyncio.run(tool.safe_execute())
    assert result.get("executed") is True


def test_degraded_manager_still_attempts():
    # Degraded (was warm) lets the call through; circuit breaker owns
    # repeated-failure protection.
    tool = _ReadTool(_FakeClient(_settings(), _FakeManager("degraded")))
    result = asyncio.run(tool.safe_execute())
    assert result.get("executed") is True
