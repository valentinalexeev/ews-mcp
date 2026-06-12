"""R1 regression tests: central id-alias wiring in BaseTool.safe_execute.

The dispatcher (a) resolves short aliases in id params before execute(),
(b) attaches ``sid`` next to long EWS ids in outputs, (c) rebinds aliases
on move remaps, (d) degrades to passthrough when disabled. Raw ids keep
working everywhere — aliasing is strictly additive.

Coroutines are driven with ``asyncio.run`` so the suite does not depend on
pytest-asyncio configuration.
"""
from __future__ import annotations

import asyncio
import os
from typing import Any, Dict

from src.config import Settings
from src.id_alias import get_aliaser
from src.tools.base import BaseTool

# Realistic EWS id lengths (~100+ chars; > the 40-char alias threshold).
RAW_ID_1 = "AAMkAGI4" + "a" * 100 + "AQA1"
RAW_ID_2 = "AAMkAGI4" + "b" * 100 + "AQA2"
RAW_CONV = "AAQkAGI4" + "c" * 100 + "AQAc"


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
        ews_offload_enabled=False,  # keep unit tests single-threaded
    )
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


class _FakeClient:
    def __init__(self, config):
        self.config = config


class _EchoTool(BaseTool):
    """Returns whatever payload it was constructed with + records kwargs."""

    side_effect_class = "read"

    def __init__(self, ews_client, payload: Dict[str, Any]):
        super().__init__(ews_client)
        self.payload = payload
        self.received: Dict[str, Any] | None = None

    def get_schema(self) -> Dict[str, Any]:
        return {
            "name": "dummy_echo",
            "description": "test-only echo tool",
            "inputSchema": {"type": "object", "properties": {}},
        }

    async def execute(self, **kwargs) -> Dict[str, Any]:
        self.received = kwargs
        return {"success": True, "message": "ok", **self.payload}


def _aliaser():
    return get_aliaser(os.environ["EWS_MEMORY_DIR"])


def test_outputs_gain_sid_for_long_ids():
    tool = _EchoTool(_FakeClient(_settings()), {
        "items": [
            {"message_id": RAW_ID_1, "subject": "A"},
            {"message_id": RAW_ID_2, "subject": "B"},
        ],
    })
    result = asyncio.run(tool.safe_execute())
    sids = [i["sid"] for i in result["items"]]
    assert sids == ["m1", "m2"]
    # Raw ids still present — aliasing is additive.
    assert result["items"][0]["message_id"] == RAW_ID_1


def test_short_ids_untouched():
    tool = _EchoTool(_FakeClient(_settings()), {"message_id": "email-1"})
    result = asyncio.run(tool.safe_execute())
    assert "sid" not in result


def test_alias_inputs_resolve_before_execute():
    alias = _aliaser().alias_for(RAW_ID_1, "m")
    tool = _EchoTool(_FakeClient(_settings()), {})
    asyncio.run(tool.safe_execute(message_id=alias))
    assert tool.received == {"message_id": RAW_ID_1}


def test_raw_id_inputs_pass_through():
    tool = _EchoTool(_FakeClient(_settings()), {})
    asyncio.run(tool.safe_execute(message_id=RAW_ID_1))
    assert tool.received == {"message_id": RAW_ID_1}


def test_stale_alias_is_clean_validation_error():
    tool = _EchoTool(_FakeClient(_settings()), {})
    result = asyncio.run(tool.safe_execute(message_id="m999"))
    assert result["success"] is False
    assert "m999" in result["error"]
    assert tool.received is None  # execute never ran


def test_ids_list_resolves_mixed():
    alias = _aliaser().alias_for(RAW_ID_1, "m")
    tool = _EchoTool(_FakeClient(_settings()), {})
    asyncio.run(tool.safe_execute(ids=[alias, RAW_ID_2]))
    assert tool.received == {"ids": [RAW_ID_1, RAW_ID_2]}


def test_move_remap_rebinds_alias():
    alias = _aliaser().alias_for(RAW_ID_1, "m")
    tool = _EchoTool(_FakeClient(_settings()), {
        "moved": [{"old_id": RAW_ID_1, "new_id": RAW_ID_2}],
    })
    result = asyncio.run(tool.safe_execute())
    assert result["moved"][0]["sid"] == alias
    # The model's alias now points at the post-move id.
    assert _aliaser().resolve(alias) == RAW_ID_2


def test_conversation_id_gets_thread_sid():
    tool = _EchoTool(_FakeClient(_settings()), {"conversation_id": RAW_CONV})
    result = asyncio.run(tool.safe_execute())
    assert result["thread_sid"] == "t1"


def test_internet_message_id_captured_for_self_heal():
    imid = "<CABc123@mail.corp.example>"
    tool = _EchoTool(_FakeClient(_settings()), {
        "message_id": RAW_ID_1, "internet_message_id": imid,
    })
    result = asyncio.run(tool.safe_execute())
    assert _aliaser().imid_for(result["sid"]) == imid


def test_aliasing_disabled_is_full_passthrough():
    tool = _EchoTool(
        _FakeClient(_settings(ews_id_aliases_enabled=False)),
        {"message_id": RAW_ID_1},
    )
    result = asyncio.run(tool.safe_execute(message_id="m1"))
    # Input "m1" passed through verbatim; no sid attached.
    assert tool.received == {"message_id": "m1"}
    assert "sid" not in result
