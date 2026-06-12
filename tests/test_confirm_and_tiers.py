"""R2 regression tests: central two-phase confirm + capability tiers.

Covers:
  * phase 1 (no token): preview + HMAC token, NO execution;
  * phase 2 (token): executes; tampered args / garbage tokens rejected;
  * kill-switch precedence over the confirm gate;
  * conditional confirm (delete_email permanent, oof set, folder delete);
  * approval-executor pre-confirmation via mint_confirm_token;
  * capability tiers read ⊂ draft ⊂ full (dispatch-level gate);
  * public_schema confirm_token injection.

Coroutines are driven with ``asyncio.run`` so the suite does not depend on
pytest-asyncio configuration.
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict

from src.config import Settings
from src.tools.base import BaseTool, public_schema, tool_uses_confirm
from src.tools.email_tools import SendEmailTool, DeleteEmailTool
from src.tools.oof_tools import OofSettingsTool
from src.tools.folder_tools import ManageFolderTool


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
        ews_offload_enabled=False,
    )
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


class _FakeClient:
    def __init__(self, config):
        self.config = config


class _ConfirmedSendTool(BaseTool):
    """Send-class tool with the central confirm gate; execute() is a
    sentinel so we can observe whether the side effect ran."""

    side_effect_class = "send"
    confirm_required = True

    def __init__(self, ews_client):
        super().__init__(ews_client)
        self.executed_with: Dict[str, Any] | None = None

    def get_schema(self) -> Dict[str, Any]:
        return {
            "name": "dummy_confirmed_send",
            "description": "test-only confirmed send tool",
            "inputSchema": {"type": "object", "properties": {}},
        }

    async def execute(self, **kwargs) -> Dict[str, Any]:
        self.executed_with = kwargs
        return {"success": True, "message": "sent", "sent": True}


class _PlainWriteTool(BaseTool):
    side_effect_class = "write"

    def get_schema(self) -> Dict[str, Any]:
        return {
            "name": "dummy_write",
            "description": "test-only write tool",
            "inputSchema": {"type": "object", "properties": {}},
        }

    async def execute(self, **kwargs) -> Dict[str, Any]:
        return {"success": True, "message": "wrote", "wrote": True}


# --- two-phase confirm --------------------------------------------------------


def test_phase1_previews_without_executing():
    tool = _ConfirmedSendTool(_FakeClient(_settings()))
    result = asyncio.run(tool.safe_execute(
        to=["x@external.example"], subject="Hi", body="B" * 500,
    ))
    assert result["requires_confirmation"] is True
    assert result["confirm_token"]
    assert result["preview"]["subject"] == "Hi"
    assert len(result["preview"]["body_preview"]) == 200
    assert any("external" in w for w in result["warnings"])
    assert tool.executed_with is None  # nothing ran


def test_phase2_executes_with_valid_token():
    tool = _ConfirmedSendTool(_FakeClient(_settings()))
    args = dict(to=["x@corp.example"], subject="Hi", body="b")
    phase1 = asyncio.run(tool.safe_execute(**args))
    result = asyncio.run(tool.safe_execute(
        **args, confirm_token=phase1["confirm_token"],
    ))
    assert result.get("sent") is True
    # The tool body never sees the token.
    assert "confirm_token" not in tool.executed_with


def test_phase2_rejects_changed_arguments():
    tool = _ConfirmedSendTool(_FakeClient(_settings()))
    phase1 = asyncio.run(tool.safe_execute(to=["a@corp.example"], subject="S", body="b"))
    result = asyncio.run(tool.safe_execute(
        to=["evil@external.example"], subject="S", body="b",
        confirm_token=phase1["confirm_token"],
    ))
    assert result["success"] is False
    assert "stale" in result["error"]
    assert tool.executed_with is None


def test_phase2_rejects_garbage_token():
    tool = _ConfirmedSendTool(_FakeClient(_settings()))
    result = asyncio.run(tool.safe_execute(to=["a@b.c"], confirm_token="nope.bad"))
    assert result["success"] is False
    assert tool.executed_with is None


def test_kill_switch_blocks_even_phase1():
    tool = _ConfirmedSendTool(_FakeClient(_settings(send_enabled=False)))
    result = asyncio.run(tool.safe_execute(to=["a@b.c"], subject="s", body="b"))
    assert result["success"] is False
    assert "SEND_ENABLED" in result["error"]
    assert "confirm_token" not in result


def test_minted_token_executes_directly():
    # The approval-executor path: human approved → executor pre-confirms.
    tool = _ConfirmedSendTool(_FakeClient(_settings()))
    args = dict(to=["a@corp.example"], subject="S", body="b")
    token = tool.mint_confirm_token(args)
    result = asyncio.run(tool.safe_execute(**args, confirm_token=token))
    assert result.get("sent") is True


# --- conditional confirm -------------------------------------------------------


def test_conditional_confirm_classification():
    client = _FakeClient(_settings())
    delete_tool = DeleteEmailTool(client)
    assert delete_tool.confirm_needed({"message_id": "x"}) is False
    assert delete_tool.confirm_needed({"message_id": "x", "permanent": True}) is True
    assert delete_tool.confirm_needed({"message_id": "x", "hard_delete": True}) is True
    oof = OofSettingsTool(client)
    assert oof.confirm_needed({"action": "get"}) is False
    assert oof.confirm_needed({"action": "set"}) is True
    folder = ManageFolderTool(client)
    assert folder.confirm_needed({"action": "create"}) is False
    assert folder.confirm_needed({"action": "delete"}) is True
    assert SendEmailTool.confirm_required is True


def test_real_send_email_phase1_no_network():
    # Phase 1 must short-circuit before any EWS access — a bare config-only
    # client is enough.
    tool = SendEmailTool(_FakeClient(_settings()))
    result = asyncio.run(tool.safe_execute(
        to=["x@y.example"], subject="s", body="b",
    ))
    assert result["requires_confirmation"] is True


# --- capability tiers ----------------------------------------------------------


def test_tier_read_blocks_write():
    tool = _PlainWriteTool(_FakeClient(_settings(ews_capability_tier="read")))
    result = asyncio.run(tool.safe_execute())
    assert result["success"] is False
    assert "tier" in result["error"]


def test_tier_draft_allows_write_blocks_send():
    settings = _settings(ews_capability_tier="draft")
    write_result = asyncio.run(_PlainWriteTool(_FakeClient(settings)).safe_execute())
    assert write_result.get("wrote") is True
    send_result = asyncio.run(_ConfirmedSendTool(_FakeClient(settings)).safe_execute(to=["a@b.c"]))
    assert send_result["success"] is False
    assert "tier" in send_result["error"]


def test_tier_full_default_allows_everything():
    result = asyncio.run(_PlainWriteTool(_FakeClient(_settings())).safe_execute())
    assert result.get("wrote") is True


# --- schema injection ----------------------------------------------------------


def test_public_schema_injects_confirm_token():
    client = _FakeClient(_settings())
    confirmed = public_schema(SendEmailTool(client))
    assert "confirm_token" in confirmed["inputSchema"]["properties"]
    conditional = public_schema(DeleteEmailTool(client))
    assert "confirm_token" in conditional["inputSchema"]["properties"]
    plain = public_schema(_PlainWriteTool(client))
    assert "confirm_token" not in plain["inputSchema"]["properties"]
    assert tool_uses_confirm(SendEmailTool(client))
    assert not tool_uses_confirm(_PlainWriteTool(client))
