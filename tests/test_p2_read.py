"""P2: get_thread assembly and get_event single-event detail."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from src.config import Settings
import src.tools.search_tools as st
from src.tools.search_tools import GetThreadTool
from src.tools.calendar_tools import GetEventTool


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
    def __init__(self, cfg, account=None):
        self.config = cfg
        self._account = account

    def get_account(self, target=None):
        return self._account


# --- get_thread -------------------------------------------------------------


def test_get_thread_assembles_chronologically(monkeypatch):
    newest_first = [
        {"message_id": "3", "from": "c@x", "subject": "RE: Q", "received_time": "2026-06-03T10:00:00", "body": "latest body"},
        {"message_id": "2", "from": "b@x", "subject": "RE: Q", "received_time": "2026-06-02T10:00:00", "body": "mid body"},
        {"message_id": "1", "from": "a@x", "subject": "Q", "received_time": "2026-06-01T10:00:00", "body": "first body"},
    ]

    async def _fake_conv(self, **kwargs):
        return {
            "success": True,
            "items": [dict(m) for m in newest_first],
            "conversation_id": "CONV1",
            "mailbox": "exec@corp.example",
        }

    monkeypatch.setattr(st.SearchByConversationTool, "execute", _fake_conv)
    tool = GetThreadTool(_FakeClient(_settings()))
    res = asyncio.run(tool.execute(message_id="X"))

    assert res["thread_id"] == "CONV1"
    assert res["message_count"] == 3
    assert res["participants"] == ["a@x", "b@x", "c@x"]
    assert [m["message_id"] for m in res["messages"]] == ["1", "2", "3"]
    assert res["date_range"] == ["2026-06-01T10:00:00", "2026-06-03T10:00:00"]
    assert res["truncated"] is False
    # bodies='latest' (default): only the newest keeps its body.
    assert "body" not in res["messages"][0]
    assert res["messages"][-1]["body"] == "latest body"
    assert res["latest"]["message_id"] == "3"


def test_get_thread_bodies_all_keeps_all(monkeypatch):
    items = [
        {"message_id": "1", "from": "a@x", "subject": "Q", "received_time": "2026-06-01T10:00:00", "body": "b1"},
        {"message_id": "2", "from": "b@x", "subject": "RE: Q", "received_time": "2026-06-02T10:00:00", "body": "b2"},
    ]

    async def _fake_conv(self, **kwargs):
        return {"success": True, "items": [dict(m) for m in items], "conversation_id": "C", "mailbox": "m"}

    monkeypatch.setattr(st.SearchByConversationTool, "execute", _fake_conv)
    tool = GetThreadTool(_FakeClient(_settings()))
    res = asyncio.run(tool.execute(conversation_id="C", bodies="all"))
    assert all("body" in m for m in res["messages"])


def test_get_thread_requires_an_id():
    tool = GetThreadTool(_FakeClient(_settings()))
    res = asyncio.run(tool.safe_execute())
    assert res["success"] is False


# --- get_event --------------------------------------------------------------


def _fake_event():
    att = SimpleNamespace(
        mailbox=SimpleNamespace(email_address="a@x", name="A"), response_type="Accept"
    )
    return SimpleNamespace(
        id="E1", uid="ICAL1", subject="Steering",
        start=SimpleNamespace(isoformat=lambda: "2026-06-15T10:00:00+03:00"),
        end=SimpleNamespace(isoformat=lambda: "2026-06-15T11:00:00+03:00"),
        location="Room 1", is_all_day=False, is_recurring=True, is_meeting=True,
        organizer=SimpleNamespace(email_address="org@x"),
        my_response_type="Organizer",
        required_attendees=[att], optional_attendees=[],
        text_body="agenda", body="agenda",
    )


def test_get_event_serializes_detail():
    event_obj = _fake_event()
    account = SimpleNamespace(calendar=SimpleNamespace(get=lambda id: event_obj))
    tool = GetEventTool(_FakeClient(_settings(), account))
    res = asyncio.run(tool.execute(item_id="E1"))
    ev = res["event"]
    assert ev["item_id"] == "E1"
    assert ev["ical_uid"] == "ICAL1"
    assert ev["subject"] == "Steering"
    assert ev["start"] == "2026-06-15T10:00:00+03:00"
    assert ev["is_recurring"] is True
    assert ev["organizer"] == "org@x"
    assert ev["required_attendees"][0]["email"] == "a@x"
    assert ev["required_attendees"][0]["response"] == "Accept"


def test_get_event_requires_id():
    account = SimpleNamespace(calendar=SimpleNamespace(get=lambda id: None))
    tool = GetEventTool(_FakeClient(_settings(), account))
    res = asyncio.run(tool.safe_execute())
    assert res["success"] is False
