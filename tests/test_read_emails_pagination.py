"""R0 regression tests: read_emails on the classified-pagination path.

The prod zero-items symptom: the old direct ``items[:max_results]``
iteration swallowed mid-iteration errors, so a throttled/broken folder
read returned ``emails: []`` with success=True. read_emails now rides
``_paginate_query`` like search_emails: full failure raises a classified
ToolExecutionError, partial failure ships collected items + meta, and
the canonical paged envelope is emitted alongside the legacy keys.

Coroutines are driven with ``asyncio.run`` so the suite does not depend on
pytest-asyncio configuration.
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from unittest.mock import MagicMock

from src.tools.email_tools import ReadEmailsTool


class _ExplodingQuery:
    """Slicing raises — simulates throttling/transport failure mid-read."""

    def only(self, *fields):
        return self

    def count(self):
        return 7

    def __getitem__(self, item):
        raise ConnectionError("Connection aborted by Exchange")


class _ListQuery(list):
    """List-backed query with .only()/.count() like a QuerySet."""

    def only(self, *fields):
        return self

    def count(self):
        return len(self)


def _mock_email(i: int):
    email = MagicMock()
    email.id = f"email-{i}"
    email.subject = f"Subject {i}"
    email.sender.email_address = "sender@example.com"
    email.datetime_received = datetime(2026, 1, 1, 10, 0, 0)
    email.is_read = False
    email.has_attachments = False
    email.text_body = f"Body {i}"
    return email


def _client_with_query(query):
    client = MagicMock()
    client.config.ews_email = "exec@corp.example"
    folder = MagicMock()
    folder.name = "Inbox"
    folder.all.return_value.order_by.return_value = query
    client.account.inbox = folder
    client.get_account.return_value = client.account
    return client


def test_read_failure_raises_classified_error_not_empty_success():
    tool = ReadEmailsTool(_client_with_query(_ExplodingQuery()))
    result = asyncio.run(tool.safe_execute(folder="inbox", max_results=10))
    # safe_execute converts the raised ToolExecutionError to an error dict.
    assert result["success"] is False
    assert "CONNECTION" in result["error"]
    assert "Inbox" in result["error"]


def test_read_success_ships_canonical_envelope_and_legacy_keys():
    query = _ListQuery(_mock_email(i) for i in range(3))
    tool = ReadEmailsTool(_client_with_query(query))

    async def run():
        return await tool.execute(folder="inbox", max_results=10)

    result = asyncio.run(run())
    assert result["success"] is True
    # Canonical paged envelope
    assert result["count"] == 3
    assert result["total_available"] == 3
    assert [i["subject"] for i in result["items"]] == [
        "Subject 0", "Subject 1", "Subject 2",
    ]
    assert "next_offset" not in result  # exhausted
    # Legacy spellings preserved (one-release window)
    assert result["total_count"] == 3
    assert result["emails"][0]["subject"] == "Subject 0"
    assert result["emails"][0]["preview"] == "Body 0"


def test_read_paginates_with_next_offset():
    query = _ListQuery(_mock_email(i) for i in range(5))
    tool = ReadEmailsTool(_client_with_query(query))

    async def run():
        return await tool.execute(folder="inbox", max_results=2, offset=0)

    result = asyncio.run(run())
    assert result["count"] == 2
    assert result["total_available"] == 5
    assert result["next_offset"] == 2


def test_unread_only_filters_via_queryset():
    inner = _ListQuery([_mock_email(0)])
    query = MagicMock()
    query.only.return_value = inner
    filtered = MagicMock()
    filtered.only.return_value = inner
    query.filter.return_value = filtered
    tool = ReadEmailsTool(_client_with_query(query))

    async def run():
        return await tool.execute(folder="inbox", unread_only=True)

    result = asyncio.run(run())
    assert result["success"] is True
    query.filter.assert_called_once_with(is_read=False)
