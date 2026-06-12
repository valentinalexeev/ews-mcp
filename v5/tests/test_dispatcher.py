"""Dispatcher gate-chain tests: every IDEATION §7-8 gate, in order.

Driven with ``asyncio.run`` against ``dispatch()`` directly.
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict

from conftest import make_settings
from ewsmcp.audit import AuditLog
from ewsmcp.ids import get_aliaser
from ewsmcp.tools.base import Context, ToolSpec, dispatch

RAW_ID = "AAMkAGI4" + "a" * 100


class _Gateway:
    async def call(self, fn):
        raise AssertionError("gateway should not be hit in these tests")


def _ctx(tmp_path, **settings_overrides) -> Context:
    return Context(
        settings=make_settings(**settings_overrides),
        gateway=_Gateway(),
        manager=None,
        aliaser=get_aliaser(str(tmp_path / "mem")),
        audit=AuditLog(str(tmp_path / "data")),
    )


def _spec(handler, *, cls="read", confirm=False, requires_ews=True, name="t") -> ToolSpec:
    return ToolSpec(
        name=name, description="test", side_effect_class=cls,
        input_schema={"type": "object", "properties": {}},
        handler=handler, requires_ews=requires_ews, confirm=confirm,
    )


async def _ok_handler(ctx, **kwargs) -> Dict[str, Any]:
    return {"ran": True, "got": kwargs}


# --- kill-switch & tier ---------------------------------------------------


def test_kill_switch_blocks_send_class(tmp_path):
    ctx = _ctx(tmp_path, send_enabled=False, ews_capability_tier="full")
    result = asyncio.run(dispatch(ctx, _spec(_ok_handler, cls="send"), {}))
    assert result["ok"] is False and result["error"]["code"] == "kill_switch"


def test_tier_draft_blocks_send_and_destructive(tmp_path):
    ctx = _ctx(tmp_path, send_enabled=True, ews_capability_tier="draft")
    for cls in ("send", "destructive"):
        result = asyncio.run(dispatch(ctx, _spec(_ok_handler, cls=cls), {}))
        assert result["error"]["code"] in ("tier_blocked", "kill_switch")
    write = asyncio.run(dispatch(ctx, _spec(_ok_handler, cls="write"), {}))
    assert write.get("ran") is True


def test_tier_read_blocks_write(tmp_path):
    ctx = _ctx(tmp_path, ews_capability_tier="read")
    result = asyncio.run(dispatch(ctx, _spec(_ok_handler, cls="write"), {}))
    assert result["error"]["code"] == "tier_blocked"


# --- cold gate --------------------------------------------------------------


class _ColdManager:
    state = "connecting"

    def status(self):
        return {"state": "connecting", "attempts": 4,
                "last_error": "TransportError: Failed to get auth type",
                "next_retry_in_s": 30, "last_success_age_s": None}


def test_cold_gate_blocks_ews_tools_with_hint(tmp_path):
    ctx = _ctx(tmp_path)
    ctx.manager = _ColdManager()
    result = asyncio.run(dispatch(ctx, _spec(_ok_handler), {}))
    assert result["error"]["code"] == "upstream_unavailable"
    assert "auth type" in result["error"]["message"]
    assert result["error"]["retry_after_s"] == 30


def test_cold_gate_passes_local_tools(tmp_path):
    ctx = _ctx(tmp_path)
    ctx.manager = _ColdManager()
    result = asyncio.run(dispatch(ctx, _spec(_ok_handler, requires_ews=False), {}))
    assert result.get("ran") is True


# --- two-phase confirm -------------------------------------------------------


def test_confirm_phase1_then_phase2(tmp_path):
    ctx = _ctx(tmp_path, send_enabled=True, ews_capability_tier="full")
    spec = _spec(_ok_handler, cls="send", confirm=True)
    args = {"to": ["x@external.example"], "subject": "s", "body": "b" * 300}
    p1 = asyncio.run(dispatch(ctx, spec, dict(args)))
    assert p1["requires_confirmation"] is True
    assert len(p1["preview"]["body"]) == 200
    assert any("external" in w for w in p1["warnings"])
    p2 = asyncio.run(dispatch(ctx, spec, {**args, "confirm_token": p1["confirm_token"]}))
    assert p2.get("ran") is True
    assert "confirm_token" not in p2["got"]


def test_confirm_rejects_changed_args(tmp_path):
    ctx = _ctx(tmp_path, send_enabled=True, ews_capability_tier="full")
    spec = _spec(_ok_handler, cls="send", confirm=True)
    p1 = asyncio.run(dispatch(ctx, spec, {"to": ["a@b.c"]}))
    p2 = asyncio.run(dispatch(ctx, spec, {"to": ["evil@x.y"],
                                          "confirm_token": p1["confirm_token"]}))
    assert p2["error"]["code"] == "confirm_invalid"


def test_conditional_confirm_callable(tmp_path):
    ctx = _ctx(tmp_path, ews_capability_tier="full")
    spec = _spec(_ok_handler, cls="destructive",
                 confirm=lambda kw: kw.get("permanent") is True)
    soft = asyncio.run(dispatch(ctx, spec, {"permanent": False}))
    assert soft.get("ran") is True
    hard = asyncio.run(dispatch(ctx, spec, {"permanent": True}))
    assert hard["requires_confirmation"] is True


# --- recipient guards & rate cap ----------------------------------------------


def test_denylist_blocks(tmp_path):
    ctx = _ctx(tmp_path, send_enabled=True, ews_capability_tier="full",
               ews_recipient_denylist="*@competitor.example")
    result = asyncio.run(dispatch(ctx, _spec(_ok_handler, cls="send"),
                                  {"to": ["ceo@competitor.example"]}))
    assert result["error"]["code"] == "recipient_blocked"


def test_rate_cap(tmp_path):
    ctx = _ctx(tmp_path, send_enabled=True, ews_capability_tier="full",
               ews_max_sends_per_hour=2)
    spec = _spec(_ok_handler, cls="send")
    assert asyncio.run(dispatch(ctx, spec, {})).get("ran") is True
    assert asyncio.run(dispatch(ctx, spec, {})).get("ran") is True
    third = asyncio.run(dispatch(ctx, spec, {}))
    assert third["error"]["code"] == "rate_capped"


# --- alias resolution ------------------------------------------------------------


def test_alias_inputs_resolved(tmp_path):
    ctx = _ctx(tmp_path)
    alias = ctx.aliaser.alias_for(RAW_ID, "m")
    result = asyncio.run(dispatch(ctx, _spec(_ok_handler), {"id": alias}))
    assert result["got"] == {"id": RAW_ID}


def test_stale_alias_clean_error(tmp_path):
    ctx = _ctx(tmp_path)
    result = asyncio.run(dispatch(ctx, _spec(_ok_handler), {"id": "m999"}))
    assert result["error"]["code"] == "validation"


# --- error mapping & circuit ---------------------------------------------------


def test_exception_mapping_throttled(tmp_path):
    class ErrorServerBusy(Exception):
        pass

    async def boom(ctx, **kwargs):
        raise ErrorServerBusy("server asked to back off 40000 ms")

    ctx = _ctx(tmp_path)
    result = asyncio.run(dispatch(ctx, _spec(boom), {}))
    assert result["error"]["code"] == "throttled"
    assert result["error"]["retry_after_s"]


def test_circuit_opens_after_threshold(tmp_path):
    async def boom(ctx, **kwargs):
        raise ConnectionError("connection reset")

    ctx = _ctx(tmp_path, circuit_failure_threshold=3, circuit_open_seconds=60)
    spec = _spec(boom)
    for _ in range(3):
        asyncio.run(dispatch(ctx, spec, {}))
    result = asyncio.run(dispatch(ctx, spec, {}))
    assert result["error"]["code"] == "upstream_unavailable"
    assert "circuit open" in result["error"]["message"]


def test_audit_chain_written(tmp_path):
    ctx = _ctx(tmp_path)
    asyncio.run(dispatch(ctx, _spec(_ok_handler), {}))
    asyncio.run(dispatch(ctx, _spec(_ok_handler), {}))
    audit_dir = tmp_path / "data" / "audit"
    files = list(audit_dir.glob("*.jsonl"))
    assert files, "audit file missing"
    lines = files[0].read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    import hashlib
    import json
    prev = "GENESIS"
    for line in lines:
        rec = json.loads(line)
        h = rec.pop("h")
        canonical = json.dumps(rec, sort_keys=True, ensure_ascii=False)
        assert h == hashlib.sha256(f"{prev}|{canonical}".encode()).hexdigest()
        prev = h
