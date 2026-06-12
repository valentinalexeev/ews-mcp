"""P1: two-phase send_draft, update_draft, and the confirm-token helper."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src.config import Settings
from src.safety import make_token, verify_token, content_hash
from src.tools.draft_send_tools import SendDraftTool, UpdateDraftTool, _draft_content_hash
import src.tools.draft_send_tools as dst


# --- confirm-token helper ---------------------------------------------------


def test_token_roundtrip_ok():
    ch = content_hash("subj", ["a@b.com"], "body")
    tok = make_token(mailbox="m@x.com", action="send_draft", target_id="ID1", chash=ch)
    ok, reason = verify_token(
        tok["confirm_token"], mailbox="m@x.com", action="send_draft", target_id="ID1", chash=ch
    )
    assert ok and reason == "ok"


def test_token_tamper_detected():
    ch = content_hash("subj")
    tok = make_token(mailbox="m@x.com", action="send_draft", target_id="ID1", chash=ch)
    bad = tok["confirm_token"][:-4] + "dead"
    ok, reason = verify_token(
        bad, mailbox="m@x.com", action="send_draft", target_id="ID1", chash=ch
    )
    assert not ok
    assert reason in ("bad_signature", "malformed")


def test_token_stale_on_content_change():
    tok = make_token(
        mailbox="m@x.com", action="send_draft", target_id="ID1", chash=content_hash("old")
    )
    ok, reason = verify_token(
        tok["confirm_token"], mailbox="m@x.com", action="send_draft", target_id="ID1",
        chash=content_hash("new"),
    )
    assert not ok and reason == "stale"


def test_token_expired():
    ch = content_hash("x")
    tok = make_token(
        mailbox="m@x.com", action="send_draft", target_id="ID1", chash=ch, ttl_seconds=-1
    )
    ok, reason = verify_token(
        tok["confirm_token"], mailbox="m@x.com", action="send_draft", target_id="ID1", chash=ch
    )
    assert not ok and reason == "expired"


def test_draft_content_hash_recipient_order_independent():
    a = _draft_content_hash("s", ["a@x", "b@x"], ["c@x"], [], "body")
    b = _draft_content_hash("s", ["b@x", "a@x"], ["c@x"], [], "body")
    assert a == b
    # Adding a recipient is a content change that must invalidate the token.
    c = _draft_content_hash("s", ["a@x", "b@x", "z@x"], ["c@x"], [], "body")
    assert c != a


# --- fakes ------------------------------------------------------------------


def _settings(**ov) -> Settings:
    base = dict(
        ews_email="exec@corp.example",
        ews_auth_type="basic",
        ews_username="u",
        ews_password="p",
        ews_autodiscover=False,
        ews_server_url="https://mail.corp.example/EWS/Exchange.asmx",
        rate_limit_enabled=False,
        enable_audit_log=False,
    )
    base.update(ov)
    return Settings(**base)  # type: ignore[arg-type]


class _FakeClient:
    def __init__(self, cfg, account):
        self.config = cfg
        self._account = account

    def get_account(self, target=None):
        return self._account


def _fake_draft(send_mock):
    return SimpleNamespace(
        subject="Q3 numbers",
        to_recipients=[SimpleNamespace(email_address="cfo@corp.example")],
        cc_recipients=[],
        bcc_recipients=[],
        text_body="Here are the numbers.",
        body="Here are the numbers.",
        attachments=[],
        message_id="<abc@corp.example>",
        send=send_mock,
    )


# --- send_draft two-phase ---------------------------------------------------


def test_send_draft_two_phase(monkeypatch):
    send_mock = Mock()
    draft = _fake_draft(send_mock)
    monkeypatch.setattr(dst, "find_message_for_account", lambda acc, mid: draft)
    tool = SendDraftTool(_FakeClient(_settings(send_enabled=True), object()))

    # Phase 1: preview only, nothing sent.
    p1 = asyncio.run(tool.execute(draft_id="D1"))
    assert p1["requires_confirmation"] is True
    assert "confirm_token" in p1
    assert send_mock.call_count == 0
    assert p1["preview"]["to"] == ["cfo@corp.example"]

    # Phase 2: send with the token.
    p2 = asyncio.run(tool.execute(draft_id="D1", confirm_token=p1["confirm_token"]))
    assert p2["sent"] is True
    assert send_mock.call_count == 1


def test_send_draft_rejects_tampered_token(monkeypatch):
    draft = _fake_draft(Mock())
    monkeypatch.setattr(dst, "find_message_for_account", lambda acc, mid: draft)
    tool = SendDraftTool(_FakeClient(_settings(send_enabled=True), object()))
    p1 = asyncio.run(tool.execute(draft_id="D1"))
    bad = p1["confirm_token"][:-4] + "dead"
    with pytest.raises(Exception):
        asyncio.run(tool.execute(draft_id="D1", confirm_token=bad))
    assert draft.send.call_count == 0


def test_send_draft_blocked_when_send_disabled():
    # Via safe_execute the kill switch returns before execute(); no draft needed.
    tool = SendDraftTool(_FakeClient(_settings(send_enabled=False), object()))
    result = asyncio.run(tool.safe_execute(draft_id="D1"))
    assert result["success"] is False
    assert "SEND_ENABLED" in result["error"]


def test_send_draft_classified_send():
    assert SendDraftTool.side_effect_class == "send"


# --- update_draft -----------------------------------------------------------


def test_update_draft_changes_fields(monkeypatch):
    save_mock = Mock()
    draft = SimpleNamespace(
        subject="old", to_recipients=[], cc_recipients=[], bcc_recipients=[],
        body="old", importance="Normal", id="ID-new", save=save_mock,
    )
    monkeypatch.setattr(dst, "find_message_for_account", lambda acc, mid: draft)
    tool = UpdateDraftTool(_FakeClient(_settings(), object()))
    result = asyncio.run(tool.execute(draft_id="D1", subject="new subject", body="hi"))
    assert result["success"] is True
    assert draft.subject == "new subject"
    assert save_mock.call_count == 1
    _, kwargs = save_mock.call_args
    assert {"subject", "body"}.issubset(set(kwargs.get("update_fields", [])))


def test_update_draft_requires_a_field(monkeypatch):
    draft = SimpleNamespace(
        subject="old", to_recipients=[], cc_recipients=[], bcc_recipients=[],
        body="old", importance="Normal", id="ID", save=Mock(),
    )
    monkeypatch.setattr(dst, "find_message_for_account", lambda acc, mid: draft)
    tool = UpdateDraftTool(_FakeClient(_settings(), object()))
    result = asyncio.run(tool.safe_execute(draft_id="D1"))
    assert result["success"] is False
