"""R2 regression tests: recipient guards, send rate cap, audit hash chain.

Coroutines are driven with ``asyncio.run`` so the suite does not depend on
pytest-asyncio configuration.
"""
from __future__ import annotations

import asyncio
import hashlib
import re
from typing import Any, Dict

import pytest

from src.config import Settings
from src.middleware.logging import AuditLogger
from src.tools.base import BaseTool, reset_send_rate_window


def _settings(**overrides) -> Settings:
    base = dict(
        ews_email="exec@acme.example",
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


class _SendTool(BaseTool):
    side_effect_class = "send"

    def __init__(self, ews_client):
        super().__init__(ews_client)
        self.sent = 0

    def get_schema(self) -> Dict[str, Any]:
        return {
            "name": "dummy_guarded_send",
            "description": "test-only send tool",
            "inputSchema": {"type": "object", "properties": {}},
        }

    async def execute(self, **kwargs) -> Dict[str, Any]:
        self.sent += 1
        return {"success": True, "message": "sent", "sent": True}


@pytest.fixture(autouse=True)
def _fresh_rate_window():
    reset_send_rate_window()
    yield
    reset_send_rate_window()


# --- recipient guards ----------------------------------------------------------


def test_denylist_blocks_matching_recipient():
    tool = _SendTool(_FakeClient(_settings(
        ews_recipient_denylist="*@competitor.example",
    )))
    result = asyncio.run(tool.safe_execute(to=["ceo@competitor.example"]))
    assert result["success"] is False
    assert "denylisted" in result["error"]
    assert tool.sent == 0


def test_allowlist_blocks_unlisted_recipient():
    tool = _SendTool(_FakeClient(_settings(
        ews_recipient_allowlist="*@acme.example",
    )))
    blocked = asyncio.run(tool.safe_execute(to=["x@gmail.example"]))
    assert blocked["success"] is False
    assert "allowlist" in blocked["error"]
    allowed = asyncio.run(tool.safe_execute(to=["colleague@acme.example"]))
    assert allowed.get("sent") is True


def test_no_guards_configured_passes():
    tool = _SendTool(_FakeClient(_settings()))
    result = asyncio.run(tool.safe_execute(to=["anyone@anywhere.example"]))
    assert result.get("sent") is True


# --- send rate cap ---------------------------------------------------------------


def test_rate_cap_blocks_after_n_sends():
    tool = _SendTool(_FakeClient(_settings(ews_max_sends_per_hour=2)))
    assert asyncio.run(tool.safe_execute()).get("sent") is True
    assert asyncio.run(tool.safe_execute()).get("sent") is True
    third = asyncio.run(tool.safe_execute())
    assert third["success"] is False
    assert "rate cap" in third["error"]
    assert tool.sent == 2


def test_rate_cap_zero_is_unlimited():
    tool = _SendTool(_FakeClient(_settings(ews_max_sends_per_hour=0)))
    for _ in range(5):
        assert asyncio.run(tool.safe_execute()).get("sent") is True


# --- audit hash chain ------------------------------------------------------------


_CHAIN_RE = re.compile(
    r"^(?P<core>.+ \| seq=(?P<seq>\d+) \| prev=[0-9A-Za-z]+) \| h=(?P<h>[0-9a-f]{64})$"
)


def _capture_lines(audit: AuditLogger, n: int = 3):
    lines = []
    audit.logger.info = lambda msg: lines.append(msg)  # type: ignore[assignment]
    audit.logger.warning = lambda msg: lines.append(msg)  # type: ignore[assignment]
    for i in range(n):
        audit.log_operation(f"op{i}", "user@x", success=(i % 2 == 0),
                            details={"i": i})
    return lines


def test_audit_records_chain_correctly():
    audit = AuditLogger()
    lines = _capture_lines(audit, 3)
    prev = "GENESIS"
    for i, line in enumerate(lines, 1):
        m = _CHAIN_RE.match(line)
        assert m, line
        assert int(m.group("seq")) == i
        expected = hashlib.sha256(
            f"{prev}|{m.group('core')}".encode("utf-8")
        ).hexdigest()
        assert m.group("h") == expected
        prev = m.group("h")


def test_audit_tamper_breaks_chain():
    audit = AuditLogger()
    lines = _capture_lines(audit, 2)
    m1 = _CHAIN_RE.match(lines[0])
    m2 = _CHAIN_RE.match(lines[1])
    tampered_core = m1.group("core").replace("op0", "opX")
    recomputed = hashlib.sha256(f"GENESIS|{tampered_core}".encode()).hexdigest()
    # The tampered record no longer matches its own hash...
    assert recomputed != m1.group("h")
    # ...and record 2 is anchored to record 1's original hash.
    expected2 = hashlib.sha256(
        f"{m1.group('h')}|{m2.group('core')}".encode("utf-8")
    ).hexdigest()
    assert m2.group("h") == expected2
