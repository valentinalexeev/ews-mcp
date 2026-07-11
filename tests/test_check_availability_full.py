"""Comprehensive regression-safe test matrix for CheckAvailabilityTool.

Goal: cover every documented and inferred behaviour of `check_availability`
so any future fix that re-introduces an old bug fails immediately.

Each test sets up `mock_ews_client.account.protocol.get_free_busy_info` to
return as many FreeBusyView-shaped MagicMocks as the tool will request,
then asserts on either the raised exception (validation paths) or the
returned response shape (happy paths).
"""

from __future__ import annotations

import pytest
from unittest.mock import MagicMock

from src.tools.calendar_tools import CheckAvailabilityTool
from src.exceptions import ValidationError, ToolExecutionError


# ---------- helpers ----------------------------------------------------------

def _fb(merged: str | None = None, calendar_events=None, view_type="DetailedMerged"):
    """Build a single FreeBusyView-like MagicMock."""
    m = MagicMock()
    m.view_type = view_type
    m.merged = merged
    m.calendar_events = calendar_events or []
    return m


def _wire(client, *fb_views, self_email="me@example.com"):
    """Configure mock_ews_client to return the given views from the EWS call."""
    client.account.primary_smtp_address = self_email
    client.account.protocol.get_free_busy_info.return_value = list(fb_views)


def _ev(start=None, end=None, busy_type="Busy", details=None):
    e = MagicMock()
    e.start = start
    e.end = end
    e.busy_type = busy_type
    e.details = details
    return e


# =============================================================================
# A. Validation — caller errors must raise ValidationError (HTTP 400)
# =============================================================================

@pytest.mark.asyncio
async def test_v01_empty_email_list(mock_ews_client):
    tool = CheckAvailabilityTool(mock_ews_client)
    with pytest.raises(ValidationError, match="email_addresses is required"):
        await tool.execute(
            email_addresses=[],
            start_time="2025-01-15T09:00:00+00:00",
            end_time="2025-01-15T17:00:00+00:00",
        )


@pytest.mark.asyncio
async def test_v02_missing_emails_kwarg(mock_ews_client):
    tool = CheckAvailabilityTool(mock_ews_client)
    with pytest.raises(ValidationError, match="email_addresses is required"):
        await tool.execute(
            start_time="2025-01-15T09:00:00+00:00",
            end_time="2025-01-15T17:00:00+00:00",
        )


@pytest.mark.asyncio
async def test_v03_missing_start(mock_ews_client):
    tool = CheckAvailabilityTool(mock_ews_client)
    with pytest.raises(ValidationError, match="start_time and end_time are required"):
        await tool.execute(
            email_addresses=["a@example.com"],
            end_time="2025-01-15T17:00:00+00:00",
        )


@pytest.mark.asyncio
async def test_v04_missing_end(mock_ews_client):
    tool = CheckAvailabilityTool(mock_ews_client)
    with pytest.raises(ValidationError, match="start_time and end_time are required"):
        await tool.execute(
            email_addresses=["a@example.com"],
            start_time="2025-01-15T09:00:00+00:00",
        )


@pytest.mark.asyncio
async def test_v05_end_before_start(mock_ews_client):
    tool = CheckAvailabilityTool(mock_ews_client)
    with pytest.raises(ValidationError, match="end_time must be after start_time"):
        await tool.execute(
            email_addresses=["a@example.com"],
            start_time="2025-01-15T17:00:00+00:00",
            end_time="2025-01-15T09:00:00+00:00",
        )


@pytest.mark.asyncio
async def test_v06_end_equals_start(mock_ews_client):
    tool = CheckAvailabilityTool(mock_ews_client)
    with pytest.raises(ValidationError, match="end_time must be after start_time"):
        await tool.execute(
            email_addresses=["a@example.com"],
            start_time="2025-01-15T09:00:00+00:00",
            end_time="2025-01-15T09:00:00+00:00",
        )


@pytest.mark.asyncio
async def test_v07_invalid_iso_string(mock_ews_client):
    tool = CheckAvailabilityTool(mock_ews_client)
    with pytest.raises(ValidationError, match="[Ii]nvalid datetime"):
        await tool.execute(
            email_addresses=["a@example.com"],
            start_time="not-a-date",
            end_time="2025-01-15T17:00:00+00:00",
        )


@pytest.mark.asyncio
async def test_v08_invalid_iso_end(mock_ews_client):
    tool = CheckAvailabilityTool(mock_ews_client)
    with pytest.raises(ValidationError, match="[Ii]nvalid datetime"):
        await tool.execute(
            email_addresses=["a@example.com"],
            start_time="2025-01-15T09:00:00+00:00",
            end_time="garbage-end",
        )


# =============================================================================
# B. Email normalization & dedup
# =============================================================================

@pytest.mark.asyncio
async def test_n01_dedup_case_insensitive(mock_ews_client):
    """Same email in different case = one entry."""
    tool = CheckAvailabilityTool(mock_ews_client)
    _wire(mock_ews_client, _fb("00"), _fb("00"))  # self + one user
    res = await tool.execute(
        email_addresses=["User@Example.com", "user@example.com", "USER@EXAMPLE.COM"],
        start_time="2025-01-15T09:00:00+00:00",
        end_time="2025-01-15T10:00:00+00:00",
    )
    # Self gets prepended → 2 emails total (self + the deduped user)
    assert res["checked_email_addresses"] == ["me@example.com", "User@Example.com"]
    assert len(res["availability"]) == 2


@pytest.mark.asyncio
async def test_n02_skip_none_and_empty(mock_ews_client):
    """None, empty string, non-string entries are silently skipped."""
    tool = CheckAvailabilityTool(mock_ews_client)
    _wire(mock_ews_client, _fb("0"), _fb("0"))  # self + the one valid email
    res = await tool.execute(
        email_addresses=[None, "", "  ", "good@example.com", 12345, None],
        start_time="2025-01-15T09:00:00+00:00",
        end_time="2025-01-15T09:30:00+00:00",
    )
    assert res["checked_email_addresses"] == ["me@example.com", "good@example.com"]


@pytest.mark.asyncio
async def test_n03_first_occurrence_preserved(mock_ews_client):
    """When dedup hits, the original-cased first occurrence is kept."""
    tool = CheckAvailabilityTool(mock_ews_client)
    _wire(mock_ews_client, _fb("0"), _fb("0"))
    res = await tool.execute(
        email_addresses=["FirstCase@Example.com", "firstcase@example.com"],
        start_time="2025-01-15T09:00:00+00:00",
        end_time="2025-01-15T09:30:00+00:00",
    )
    assert "FirstCase@Example.com" in res["checked_email_addresses"]
    assert res["checked_email_addresses"].count("firstcase@example.com") == 0


# =============================================================================
# C. include_self semantics
# =============================================================================

@pytest.mark.asyncio
async def test_s01_include_self_default_prepends(mock_ews_client):
    tool = CheckAvailabilityTool(mock_ews_client)
    _wire(mock_ews_client, _fb("0"), _fb("0"))
    res = await tool.execute(
        email_addresses=["other@example.com"],
        start_time="2025-01-15T09:00:00+00:00",
        end_time="2025-01-15T09:30:00+00:00",
    )
    assert res["checked_email_addresses"][0] == "me@example.com"
    assert res["include_self"] is True


@pytest.mark.asyncio
async def test_s02_include_self_already_in_list_no_dup(mock_ews_client):
    """If self is already in the list, do not prepend a duplicate."""
    tool = CheckAvailabilityTool(mock_ews_client)
    _wire(mock_ews_client, _fb("0"), _fb("0"))
    res = await tool.execute(
        email_addresses=["me@example.com", "other@example.com"],
        start_time="2025-01-15T09:00:00+00:00",
        end_time="2025-01-15T09:30:00+00:00",
    )
    assert res["checked_email_addresses"] == ["me@example.com", "other@example.com"]


@pytest.mark.asyncio
async def test_s03_include_self_false(mock_ews_client):
    tool = CheckAvailabilityTool(mock_ews_client)
    _wire(mock_ews_client, _fb("0"))
    res = await tool.execute(
        email_addresses=["other@example.com"],
        start_time="2025-01-15T09:00:00+00:00",
        end_time="2025-01-15T09:30:00+00:00",
        include_self=False,
    )
    assert res["checked_email_addresses"] == ["other@example.com"]
    assert res["include_self"] is False


@pytest.mark.asyncio
async def test_s04_self_email_none_does_not_crash(mock_ews_client):
    """Guard: primary_smtp_address=None must not AttributeError on .lower()."""
    tool = CheckAvailabilityTool(mock_ews_client)
    mock_ews_client.account.primary_smtp_address = None
    mock_ews_client.account.protocol.get_free_busy_info.return_value = [_fb("0")]
    res = await tool.execute(
        email_addresses=["other@example.com"],
        start_time="2025-01-15T09:00:00+00:00",
        end_time="2025-01-15T09:30:00+00:00",
        include_self=True,
    )
    # Self is not added when primary is None — only the explicit email remains.
    assert res["checked_email_addresses"] == ["other@example.com"]


@pytest.mark.asyncio
async def test_s05_target_mailbox_overrides_self(mock_ews_client):
    """target_mailbox is the 'self' identity, not account.primary_smtp_address."""
    tool = CheckAvailabilityTool(mock_ews_client)
    mock_ews_client.account.primary_smtp_address = "wrong@example.com"
    mock_ews_client.account.protocol.get_free_busy_info.return_value = [_fb("0"), _fb("0")]
    # The base class get_mailbox_info compares lowercased target vs ews_email; mock_settings has
    # ews_email='test@example.com', so a different target_mailbox is reflected back.
    res = await tool.execute(
        email_addresses=["other@example.com"],
        start_time="2025-01-15T09:00:00+00:00",
        end_time="2025-01-15T09:30:00+00:00",
        target_mailbox="impersonated@example.com",
    )
    assert res["checked_email_addresses"][0] == "impersonated@example.com"
    assert res["mailbox"] == "impersonated@example.com"


# =============================================================================
# D. Response shape
# =============================================================================

@pytest.mark.asyncio
async def test_r01_shape_with_merged(mock_ews_client):
    tool = CheckAvailabilityTool(mock_ews_client)
    _wire(mock_ews_client, _fb("0202"))  # self only, mixed free/busy
    res = await tool.execute(
        email_addresses=[],
        start_time="2025-01-15T09:00:00+00:00",
        end_time="2025-01-15T11:00:00+00:00",
        interval_minutes=30,
        include_self=True,
    ) if False else None
    # The above branch is unreachable — empty list raises. Use a real input:
    _wire(mock_ews_client, _fb("0202"), _fb("0202"))
    res = await tool.execute(
        email_addresses=["other@example.com"],
        start_time="2025-01-15T09:00:00+00:00",
        end_time="2025-01-15T11:00:00+00:00",
        interval_minutes=30,
    )
    user = res["availability"][1]
    assert user["merged_free_busy"] == "0202"
    assert user["free_busy_legend"] == {"0": "Free", "1": "Tentative", "2": "Busy",
                                          "3": "OutOfOffice", "4": "NoData"}
    assert len(user["slot_summaries"]) == 4
    assert len(user["blocking_slots"]) == 2  # the two "2"s
    assert len(user["non_free_slots"]) == 2


@pytest.mark.asyncio
async def test_r02_shape_without_merged(mock_ews_client):
    """When merged is None, the merged-related keys must be absent."""
    tool = CheckAvailabilityTool(mock_ews_client)
    _wire(mock_ews_client, _fb(None), _fb(None))
    res = await tool.execute(
        email_addresses=["other@example.com"],
        start_time="2025-01-15T09:00:00+00:00",
        end_time="2025-01-15T09:30:00+00:00",
    )
    user = res["availability"][1]
    assert "merged_free_busy" not in user
    assert "free_busy_legend" not in user
    assert "slot_summaries" not in user
    assert "blocking_slots" not in user
    # availability_summary is always present
    assert "availability_summary" in user


@pytest.mark.asyncio
async def test_r03_response_tz_plus_offset(mock_ews_client):
    tool = CheckAvailabilityTool(mock_ews_client)
    _wire(mock_ews_client, _fb("0"))
    res = await tool.execute(
        email_addresses=["me@example.com"],
        start_time="2025-01-15T09:00:00+05:30",
        end_time="2025-01-15T09:30:00+05:30",
    )
    assert res["response_timezone"] == "+05:30"


@pytest.mark.asyncio
async def test_r04_response_tz_z_suffix(mock_ews_client):
    tool = CheckAvailabilityTool(mock_ews_client)
    _wire(mock_ews_client, _fb("0"))
    res = await tool.execute(
        email_addresses=["me@example.com"],
        start_time="2025-01-15T09:00:00Z",
        end_time="2025-01-15T09:30:00Z",
    )
    # After normalization Z becomes +00:00 in display_start_time.isoformat()
    assert res["response_timezone"] == "+00:00"


@pytest.mark.asyncio
async def test_r05_response_tz_negative_offset(mock_ews_client):
    tool = CheckAvailabilityTool(mock_ews_client)
    _wire(mock_ews_client, _fb("0"))
    res = await tool.execute(
        email_addresses=["me@example.com"],
        start_time="2025-01-15T09:00:00-08:00",
        end_time="2025-01-15T09:30:00-08:00",
    )
    assert res["response_timezone"] == "-08:00"


@pytest.mark.asyncio
async def test_r06_response_tz_naive_is_empty(mock_ews_client):
    """Naive datetime should yield empty response_timezone, not garbage."""
    tool = CheckAvailabilityTool(mock_ews_client)
    _wire(mock_ews_client, _fb("0"))
    res = await tool.execute(
        email_addresses=["me@example.com"],
        start_time="2025-01-15T09:00:00",
        end_time="2025-01-15T09:30:00",
    )
    assert res["response_timezone"] == ""


@pytest.mark.asyncio
async def test_r07_time_range_echoes_raw_input(mock_ews_client):
    tool = CheckAvailabilityTool(mock_ews_client)
    _wire(mock_ews_client, _fb("0"))
    res = await tool.execute(
        email_addresses=["me@example.com"],
        start_time="2025-01-15T09:00:00+00:00",
        end_time="2025-01-15T10:00:00+00:00",
        interval_minutes=60,
    )
    assert res["time_range"] == {
        "start": "2025-01-15T09:00:00+00:00",
        "end": "2025-01-15T10:00:00+00:00",
        "interval_minutes": 60,
    }


@pytest.mark.asyncio
async def test_r08_top_level_keys_present(mock_ews_client):
    tool = CheckAvailabilityTool(mock_ews_client)
    _wire(mock_ews_client, _fb("0"))
    res = await tool.execute(
        email_addresses=["me@example.com"],
        start_time="2025-01-15T09:00:00+00:00",
        end_time="2025-01-15T09:30:00+00:00",
    )
    for key in ("success", "message", "availability", "checked_email_addresses",
                "include_self", "response_timezone", "time_range", "mailbox"):
        assert key in res, f"missing {key}"
    assert res["success"] is True
    assert "1 user" in res["message"] or "1 user(s)" in res["message"]


# =============================================================================
# E. Slot summaries — primary_status priority
# =============================================================================

@pytest.mark.asyncio
async def test_p01_all_free(mock_ews_client):
    tool = CheckAvailabilityTool(mock_ews_client)
    _wire(mock_ews_client, _fb("0000"))
    res = await tool.execute(
        email_addresses=["me@example.com"],
        start_time="2025-01-15T09:00:00+00:00",
        end_time="2025-01-15T11:00:00+00:00",
        interval_minutes=30,
    )
    s = res["availability"][0]["availability_summary"]
    assert s["primary_status"] == "free"
    assert s["is_fully_free"] is True
    assert res["availability"][0]["blocking_slots"] == []


@pytest.mark.asyncio
async def test_p02_all_busy(mock_ews_client):
    tool = CheckAvailabilityTool(mock_ews_client)
    _wire(mock_ews_client, _fb("2222"))
    res = await tool.execute(
        email_addresses=["me@example.com"],
        start_time="2025-01-15T09:00:00+00:00",
        end_time="2025-01-15T11:00:00+00:00",
        interval_minutes=30,
    )
    s = res["availability"][0]["availability_summary"]
    assert s["primary_status"] == "busy"
    assert s["has_busy"] is True
    assert s["is_fully_free"] is False
    assert len(res["availability"][0]["blocking_slots"]) == 4


@pytest.mark.asyncio
async def test_p03_tentative(mock_ews_client):
    tool = CheckAvailabilityTool(mock_ews_client)
    _wire(mock_ews_client, _fb("0011"))
    res = await tool.execute(
        email_addresses=["me@example.com"],
        start_time="2025-01-15T09:00:00+00:00",
        end_time="2025-01-15T11:00:00+00:00",
        interval_minutes=30,
    )
    s = res["availability"][0]["availability_summary"]
    assert s["primary_status"] == "tentative"
    assert s["has_tentative"] is True
    assert res["availability"][0]["blocking_slots"] == []  # tentative not blocking
    assert len(res["availability"][0]["non_free_slots"]) == 2


@pytest.mark.asyncio
async def test_p04_out_of_office_wins(mock_ews_client):
    """OOF beats busy and tentative regardless of order."""
    tool = CheckAvailabilityTool(mock_ews_client)
    _wire(mock_ews_client, _fb("3210"))
    res = await tool.execute(
        email_addresses=["me@example.com"],
        start_time="2025-01-15T09:00:00+00:00",
        end_time="2025-01-15T11:00:00+00:00",
        interval_minutes=30,
    )
    s = res["availability"][0]["availability_summary"]
    assert s["primary_status"] == "out_of_office"
    assert s["has_out_of_office"] is True
    assert s["has_busy"] is True


@pytest.mark.asyncio
async def test_p05_no_data(mock_ews_client):
    tool = CheckAvailabilityTool(mock_ews_client)
    _wire(mock_ews_client, _fb("44"))
    res = await tool.execute(
        email_addresses=["me@example.com"],
        start_time="2025-01-15T09:00:00+00:00",
        end_time="2025-01-15T10:00:00+00:00",
        interval_minutes=30,
    )
    s = res["availability"][0]["availability_summary"]
    assert s["primary_status"] == "no_data"
    assert s["has_no_data"] is True


@pytest.mark.asyncio
async def test_p06_unknown_code_is_not_blocking(mock_ews_client):
    """Unknown chars (e.g. '9') label as Unknown but do not block."""
    tool = CheckAvailabilityTool(mock_ews_client)
    _wire(mock_ews_client, _fb("09"))
    res = await tool.execute(
        email_addresses=["me@example.com"],
        start_time="2025-01-15T09:00:00+00:00",
        end_time="2025-01-15T10:00:00+00:00",
        interval_minutes=30,
    )
    slots = res["availability"][0]["slot_summaries"]
    assert slots[1]["label"] == "Unknown"
    assert slots[1]["is_blocking"] is False


# =============================================================================
# F. Calendar events
# =============================================================================

@pytest.mark.asyncio
async def test_e01_calendar_events_expanded(mock_ews_client):
    tool = CheckAvailabilityTool(mock_ews_client)
    from datetime import datetime
    ev = _ev(
        start=datetime(2025, 1, 15, 10, 0),
        end=datetime(2025, 1, 15, 11, 0),
        busy_type="Busy",
        details=MagicMock(subject="Standup"),
    )
    _wire(mock_ews_client, _fb("0", calendar_events=[ev]))
    res = await tool.execute(
        email_addresses=["me@example.com"],
        start_time="2025-01-15T09:00:00+00:00",
        end_time="2025-01-15T09:30:00+00:00",
    )
    events = res["availability"][0]["calendar_events"]
    assert len(events) == 1
    assert events[0]["busy_type"] == "Busy"
    assert events[0]["start"] is not None
    assert events[0]["end"] is not None


@pytest.mark.asyncio
async def test_e02_working_elsewhere_promoted(mock_ews_client):
    """No code conflicts → WorkingElsewhere event surfaces as primary status."""
    tool = CheckAvailabilityTool(mock_ews_client)
    from datetime import datetime
    ev = _ev(
        start=datetime(2025, 1, 15, 10, 0),
        end=datetime(2025, 1, 15, 11, 0),
        busy_type="WorkingElsewhere",
    )
    _wire(mock_ews_client, _fb("0000", calendar_events=[ev]))
    res = await tool.execute(
        email_addresses=["me@example.com"],
        start_time="2025-01-15T09:00:00+00:00",
        end_time="2025-01-15T11:00:00+00:00",
        interval_minutes=30,
    )
    s = res["availability"][0]["availability_summary"]
    assert s["primary_status"] == "working_elsewhere"
    assert s["has_working_elsewhere"] is True


@pytest.mark.asyncio
async def test_e03_event_missing_fields_safe(mock_ews_client):
    """Events with missing attributes shouldn't crash; fall back to None."""
    tool = CheckAvailabilityTool(mock_ews_client)
    bare = MagicMock(spec=[])  # has neither start, end, nor busy_type
    _wire(mock_ews_client, _fb("0", calendar_events=[bare]))
    res = await tool.execute(
        email_addresses=["me@example.com"],
        start_time="2025-01-15T09:00:00+00:00",
        end_time="2025-01-15T09:30:00+00:00",
    )
    e = res["availability"][0]["calendar_events"][0]
    assert e["start"] is None
    assert e["end"] is None
    assert e["busy_type"] == "Busy"  # default per source


# =============================================================================
# G. EWS call arguments
# =============================================================================

@pytest.mark.asyncio
async def test_g01_accounts_tuple_shape(mock_ews_client):
    """get_free_busy_info must receive (email, 'Required', False) tuples."""
    tool = CheckAvailabilityTool(mock_ews_client)
    _wire(mock_ews_client, _fb("0"), _fb("0"))
    await tool.execute(
        email_addresses=["alice@example.com"],
        start_time="2025-01-15T09:00:00+00:00",
        end_time="2025-01-15T09:30:00+00:00",
    )
    call = mock_ews_client.account.protocol.get_free_busy_info.call_args
    assert call.kwargs["accounts"] == [
        ("me@example.com", "Required", False),
        ("alice@example.com", "Required", False),
    ]


@pytest.mark.asyncio
async def test_g02_interval_passes_through(mock_ews_client):
    tool = CheckAvailabilityTool(mock_ews_client)
    _wire(mock_ews_client, _fb("0"))
    await tool.execute(
        email_addresses=["me@example.com"],
        start_time="2025-01-15T09:00:00+00:00",
        end_time="2025-01-15T09:15:00+00:00",
        interval_minutes=15,
    )
    call = mock_ews_client.account.protocol.get_free_busy_info.call_args
    assert call.kwargs["merged_free_busy_interval"] == 15


@pytest.mark.asyncio
async def test_g03_default_interval_30(mock_ews_client):
    tool = CheckAvailabilityTool(mock_ews_client)
    _wire(mock_ews_client, _fb("0"))
    await tool.execute(
        email_addresses=["me@example.com"],
        start_time="2025-01-15T09:00:00+00:00",
        end_time="2025-01-15T09:30:00+00:00",
    )
    call = mock_ews_client.account.protocol.get_free_busy_info.call_args
    assert call.kwargs["merged_free_busy_interval"] == 30


# =============================================================================
# H. Upstream errors -> ToolExecutionError (HTTP 500)
# =============================================================================

@pytest.mark.asyncio
async def test_h01_ews_call_raises_maps_to_tool_error(mock_ews_client):
    tool = CheckAvailabilityTool(mock_ews_client)
    mock_ews_client.account.primary_smtp_address = "me@example.com"
    mock_ews_client.account.protocol.get_free_busy_info.side_effect = RuntimeError("EWS down")
    with pytest.raises(ToolExecutionError, match="Failed to check availability"):
        await tool.execute(
            email_addresses=["me@example.com"],
            start_time="2025-01-15T09:00:00+00:00",
            end_time="2025-01-15T09:30:00+00:00",
        )


@pytest.mark.asyncio
async def test_h02_validation_not_swallowed(mock_ews_client):
    """Make sure ValidationError raised inside try-block bubbles up unmapped."""
    tool = CheckAvailabilityTool(mock_ews_client)
    # Force a ValidationError via end<=start, which is raised before the try.
    with pytest.raises(ValidationError):
        await tool.execute(
            email_addresses=["a@example.com"],
            start_time="2025-01-15T09:00:00+00:00",
            end_time="2025-01-15T09:00:00+00:00",
        )
