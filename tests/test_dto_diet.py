"""R1 regression tests: token-lean DTO wiring (body cleaning in read paths).

Covers:
  * list snippets show the latest reply, not the quoted chain;
  * get_email_details format="clean" strips quoted history + drops raw
    body_html (and default "full" keeps the legacy shape);
  * get_thread strips per-message quoted history by default
    (clean_bodies=false restores raw bodies).

Coroutines are driven with ``asyncio.run`` so the suite does not depend on
pytest-asyncio configuration.
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from unittest.mock import MagicMock, patch

from src.tools.email_tools import GetEmailDetailsTool, _build_list_item
from src.tools.search_tools import GetThreadTool

REPLY = "Approved — please proceed with the revised timeline."
QUOTED_CHAIN = (
    f"{REPLY}\n\n"
    "From: Ahmed Example <ahmed@example.com>\n"
    "Sent: Tuesday, June 9, 2026 2:15 PM\n"
    "To: Omar Example <omar@example.com>\n"
    "Subject: RE: RFP timeline\n\n"
    "Dear Omar,\n\n" + ("Here is a very long quoted paragraph. " * 40)
)


def _mock_message(body_text: str):
    email = MagicMock()
    email.id = "AAMkAGI4" + "z" * 100
    email.subject = "RE: RFP timeline"
    email.sender.email_address = "ahmed@example.com"
    email.sender.name = "Ahmed"
    email.to_recipients = []
    email.cc_recipients = []
    email.datetime_received = datetime(2026, 6, 10, 9, 0, 0)
    email.datetime_sent = datetime(2026, 6, 10, 8, 59, 0)
    email.is_read = True
    email.has_attachments = False
    email.importance = "Normal"
    email.categories = []
    email.attachments = []
    email.text_body = body_text
    email.message_id = "<imid-123@example.com>"
    email.body = "<html><body>raw html</body></html>"
    return email


def test_list_snippet_shows_reply_not_quoted_chain():
    card = _build_list_item(
        _mock_message(QUOTED_CHAIN),
        fields=["message_id", "subject", "snippet"],
        folder_name="Inbox",
    )
    assert card["snippet"].startswith("Approved")
    assert "very long quoted paragraph" not in card["snippet"]
    # The chain header must be stripped, not merely truncated away.
    assert "From:" not in card["snippet"]
    assert card["snippet"].rstrip() == REPLY


def test_details_clean_format_strips_history_and_html():
    client = MagicMock()
    client.config.ews_email = "exec@corp.example"
    client.get_account.return_value = client.account
    tool = GetEmailDetailsTool(client)
    with patch(
        "src.tools.email_tools.find_message_for_account",
        return_value=_mock_message(QUOTED_CHAIN),
    ):
        result = asyncio.run(tool.execute(message_id="x" * 50, format="clean"))
    email = result["email"]
    assert email["body"].startswith("Approved")
    assert "very long quoted paragraph" not in email["body"]
    assert "body_html" not in email
    assert "quoted_history" in email
    assert email["internet_message_id"] == "<imid-123@example.com>"


def test_details_default_full_keeps_legacy_shape():
    client = MagicMock()
    client.config.ews_email = "exec@corp.example"
    client.get_account.return_value = client.account
    tool = GetEmailDetailsTool(client)
    with patch(
        "src.tools.email_tools.find_message_for_account",
        return_value=_mock_message(QUOTED_CHAIN),
    ):
        result = asyncio.run(tool.execute(message_id="x" * 50))
    email = result["email"]
    assert email["body"] == QUOTED_CHAIN  # untouched
    assert "body_html" in email


def _thread_tool_with_items(items):
    client = MagicMock()
    client.config.ews_email = "exec@corp.example"
    tool = GetThreadTool(client)

    async def fake_conv_execute(self, **kwargs):
        return {"items": items, "conversation_id": "conv-1", "mailbox": "m"}

    return tool, fake_conv_execute


def test_thread_bodies_cleaned_by_default():
    items = [
        {"message_id": "a", "from": "x@y", "received_time": "2026-06-09T10:00:00",
         "subject": "RFP", "body": "First message body."},
        {"message_id": "b", "from": "z@y", "received_time": "2026-06-10T10:00:00",
         "subject": "RE: RFP", "body": QUOTED_CHAIN},
    ]
    tool, fake = _thread_tool_with_items(items)
    with patch("src.tools.search_tools.SearchByConversationTool.execute", fake):
        result = asyncio.run(tool.execute(conversation_id="conv-1", bodies="all"))
    latest = result["messages"][-1]
    assert latest["body"].startswith("Approved")
    assert "very long quoted paragraph" not in latest["body"]
    assert latest["quoted_stripped"] >= 1


def test_thread_clean_bodies_false_keeps_raw():
    items = [
        {"message_id": "b", "from": "z@y", "received_time": "2026-06-10T10:00:00",
         "subject": "RE: RFP", "body": QUOTED_CHAIN},
    ]
    tool, fake = _thread_tool_with_items(items)
    with patch("src.tools.search_tools.SearchByConversationTool.execute", fake):
        result = asyncio.run(tool.execute(
            conversation_id="conv-1", bodies="all", clean_bodies=False,
        ))
    assert result["messages"][-1]["body"] == QUOTED_CHAIN
