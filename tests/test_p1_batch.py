"""P1: batch update_messages / move_messages / delete_messages."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src.config import Settings
import src.tools.batch_email_tools as bet
from src.tools.batch_email_tools import (
    UpdateMessagesTool,
    MoveMessagesTool,
    DeleteMessagesTool,
)


def _settings(**ov) -> Settings:
    base = dict(
        ews_email="exec@corp.example", ews_auth_type="basic", ews_username="u",
        ews_password="p", ews_autodiscover=False,
        ews_server_url="https://mail.corp.example/EWS/Exchange.asmx",
        rate_limit_enabled=False, enable_audit_log=False,
    )
    base.update(ov)
    return Settings(**base)  # type: ignore[arg-type]


class _FakeClient:
    def __init__(self, cfg, account):
        self.config = cfg
        self._account = account

    def get_account(self, target=None):
        return self._account


def _patch_find(monkeypatch):
    created = []

    def _find(acc, mid):
        it = SimpleNamespace(
            id=f"new-{mid}", is_read=None, categories=None, importance=None,
            flag_status_value=None, move=Mock(), delete=Mock(), save=Mock(),
        )
        created.append(it)
        return it

    monkeypatch.setattr(bet, "find_message_for_account", _find)
    return created


async def _fake_resolve(acc, ident):
    return SimpleNamespace(name="Archive")


# --- update_messages --------------------------------------------------------


def test_update_messages_bulk(monkeypatch):
    created = _patch_find(monkeypatch)
    tool = UpdateMessagesTool(_FakeClient(_settings(), object()))
    res = asyncio.run(tool.execute(message_ids=["A", "B"], is_read=True))
    assert res["count"] == 2 and res["ok_count"] == 2
    assert len(created) == 2
    assert all(it.is_read is True for it in created)
    assert all(it.save.call_count == 1 for it in created)


def test_update_messages_requires_a_field(monkeypatch):
    _patch_find(monkeypatch)
    tool = UpdateMessagesTool(_FakeClient(_settings(), object()))
    res = asyncio.run(tool.safe_execute(message_ids=["A"]))
    assert res["success"] is False


def test_update_messages_rejects_empty_ids():
    tool = UpdateMessagesTool(_FakeClient(_settings(), object()))
    res = asyncio.run(tool.safe_execute(message_ids=[], is_read=True))
    assert res["success"] is False


# --- move_messages ----------------------------------------------------------


def test_move_messages_returns_id_remap(monkeypatch):
    created = _patch_find(monkeypatch)
    monkeypatch.setattr(bet, "resolve_folder_for_account", _fake_resolve)
    tool = MoveMessagesTool(_FakeClient(_settings(), object()))
    res = asyncio.run(tool.execute(message_ids=["A", "B"], destination_folder="Archive"))
    assert res["count"] == 2 and res["ok_count"] == 2
    assert res["destination_folder"] == "Archive"
    assert {r["new_id"] for r in res["results"]} == {"new-A", "new-B"}
    assert all(it.move.call_count == 1 for it in created)


# --- delete_messages --------------------------------------------------------


def test_delete_messages_soft(monkeypatch):
    created = _patch_find(monkeypatch)
    account = SimpleNamespace(trash=object())
    tool = DeleteMessagesTool(_FakeClient(_settings(), account))
    res = asyncio.run(tool.execute(message_ids=["A", "B"]))
    assert res["ok_count"] == 2 and res["permanent"] is False
    assert all(it.move.call_count == 1 for it in created)
    assert all(it.delete.call_count == 0 for it in created)


def test_delete_messages_hard_two_phase(monkeypatch):
    account = SimpleNamespace(trash=object())
    tool = DeleteMessagesTool(_FakeClient(_settings(), account))

    _patch_find(monkeypatch)  # phase 1 does not fetch items
    p1 = asyncio.run(tool.execute(message_ids=["A", "B"], permanent=True))
    assert p1["requires_confirmation"] is True
    assert "confirm_token" in p1

    created = _patch_find(monkeypatch)
    p2 = asyncio.run(
        tool.execute(message_ids=["A", "B"], permanent=True, confirm_token=p1["confirm_token"])
    )
    assert p2["ok_count"] == 2 and p2["permanent"] is True
    assert all(it.delete.call_count == 1 for it in created)


def test_delete_messages_hard_rejects_stale_token(monkeypatch):
    account = SimpleNamespace(trash=object())
    tool = DeleteMessagesTool(_FakeClient(_settings(), account))
    _patch_find(monkeypatch)
    p1 = asyncio.run(tool.execute(message_ids=["A", "B"], permanent=True))
    with pytest.raises(Exception):
        asyncio.run(
            tool.execute(message_ids=["A", "C"], permanent=True, confirm_token=p1["confirm_token"])
        )


def test_batch_classes_side_effects():
    assert UpdateMessagesTool.side_effect_class == "write"
    assert MoveMessagesTool.side_effect_class == "write"
    assert DeleteMessagesTool.side_effect_class == "destructive"
