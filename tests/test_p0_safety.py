"""P0 hardening regression tests.

Covers the three P0 changes:
  * SEND_ENABLED global kill-switch enforced centrally in
    ``BaseTool.safe_execute`` via ``side_effect_class == "send"``.
  * The ``whoami`` self-report tool (config-only path, no network).
  * NTLM ``auth_type`` pinning in ``EWSClient._explicit_auth_type``.

Coroutines are driven with ``asyncio.run`` so the suite does not depend on
pytest-asyncio configuration.
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict

from exchangelib import NTLM, BASIC

from src.config import Settings
from src.auth import AuthHandler
from src.ews_client import EWSClient
from src.tools.base import BaseTool
from src.tools.email_tools import SendEmailTool, ReplyEmailTool, ForwardEmailTool
from src.tools.calendar_tools import RespondToMeetingTool
from src.tools.meta_tools import WhoamiTool


class _FakeClient:
    """Minimal EWSClient stand-in. The send-gate and whoami(no-probe) paths
    only touch ``.config``."""

    def __init__(self, config):
        self.config = config


class _DummySendTool(BaseTool):
    """A send-class tool whose execute() is a no-op sentinel — lets us test
    the gate-open path without an exception (which would trip the global
    circuit breaker and pollute other tests)."""

    side_effect_class = "send"

    def get_schema(self) -> Dict[str, Any]:
        return {
            "name": "dummy_send",
            "description": "test-only send tool",
            "inputSchema": {"type": "object", "properties": {}},
        }

    async def execute(self, **kwargs) -> Dict[str, Any]:
        return {"success": True, "message": "executed", "executed": True}


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


# --- send kill-switch -------------------------------------------------------


def test_send_blocked_when_send_disabled():
    # Real send tool: the gate returns before execute(), so no Exchange call
    # and no circuit-breaker failure is recorded.
    tool = SendEmailTool(_FakeClient(_settings(send_enabled=False)))
    result = asyncio.run(tool.safe_execute(to=["a@b.com"], subject="s", body="b"))
    assert result["success"] is False
    assert "SEND_ENABLED" in result["error"]


def test_send_gate_open_when_send_enabled():
    tool = _DummySendTool(_FakeClient(_settings(send_enabled=True)))
    result = asyncio.run(tool.safe_execute())
    assert result.get("executed") is True


def test_send_tools_classified_send():
    assert SendEmailTool.side_effect_class == "send"
    assert ReplyEmailTool.side_effect_class == "send"
    assert ForwardEmailTool.side_effect_class == "send"
    assert RespondToMeetingTool.side_effect_class == "send"


def test_read_class_is_default_not_send():
    # Sanity: a default tool is not "send" and therefore never gated.
    assert BaseTool.side_effect_class == "write"
    assert WhoamiTool.side_effect_class == "read"


# --- whoami -----------------------------------------------------------------


def test_whoami_reports_config_without_network():
    tool = WhoamiTool(_FakeClient(_settings(send_enabled=False)), {})
    result = asyncio.run(tool.execute(probe_connection=False))
    assert result["success"] is True
    assert result["backend"] == "ews"
    assert result["primary_mailbox"] == "exec@corp.example"
    assert result["send_enabled"] is False
    assert result["connection"]["probed"] is False
    # On-prem endpoint → retirement advisory does not apply.
    assert result["ews_sunset"]["applies"] is False


def test_whoami_flags_exchange_online_sunset():
    tool = WhoamiTool(
        _FakeClient(_settings(ews_server_url="https://outlook.office365.com/EWS/Exchange.asmx")),
        {},
    )
    result = asyncio.run(tool.execute(probe_connection=False))
    assert result["ews_sunset"]["applies"] is True
    assert result["ews_sunset"]["removed_after"] == "2027-04-01"


# --- NTLM auth_type pin -----------------------------------------------------


def test_explicit_auth_type_pins_basic_and_ntlm():
    # Both on-prem schemes are pinned to skip the fragile auth-type
    # auto-detection probe; OAuth2 is inferred from OAuth2Credentials.
    cfg_ntlm = _settings(ews_auth_type="ntlm")
    assert EWSClient(cfg_ntlm, AuthHandler(cfg_ntlm))._explicit_auth_type() is NTLM

    cfg_basic = _settings(ews_auth_type="basic")
    assert EWSClient(cfg_basic, AuthHandler(cfg_basic))._explicit_auth_type() is BASIC
