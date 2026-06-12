"""Tool pack: calendar + people + status — IDEATION §5 tools 7-12.

Six read-class tools: list_events, get_event, check_availability,
find_people, get_oof_settings, get_server_status. Handlers receive RAW
EWS ids (the dispatcher already resolved aliases) and never implement
safety logic — gates live in tools/base.dispatch.

exchangelib 5.0.3 shapes verified against the installed library:
- ``CalendarFolder.view(start=, end=)`` expands recurrences;
  ``.filter()`` returns recurrence MASTERS only — never use it here.
- ``protocol.get_free_busy_info(accounts, start, end)`` where accounts
  is ``[(email_or_account, attendee_type, exclude_conflicts), ...]``
  e.g. ``("a@x", "Required", False)``; yields FreeBusyView objects with
  ``.calendar_events`` (CalendarEvent: start, end, busy_type).
- ``protocol.resolve_names([q], return_full_contact_data=True)`` yields
  ``(Mailbox, Contact)`` tuples (either element may be None) or plain
  Mailbox objects, and may include exception instances (e.g.
  ErrorNameResolutionNoResults) — those mean "no match", skip them.
"""

import re
import time
from datetime import datetime, time as dtime, timedelta
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from .. import __version__
from ..bodyclean import clean_body, html_to_text
from ..dto import envelope, event_card, fmt_dt
from ..errors import ToolError
from .base import Context, ToolSpec

GRID_MINUTES = 30  # free/busy slot granularity
WORKING_HOURS = (dtime(9, 0), dtime(17, 0))
_REL_RE = re.compile(r"^\+(\d{1,4})d$")


# ---------------------------------------------------------------- dates


def _parse_when(value: Any, field: str, tz_name: str) -> datetime:
    """'today' → local midnight; '+Nd' → today+N days midnight; ISO date
    → midnight; ISO datetime → as given (naive gets the server tz)."""
    tz = ZoneInfo(tz_name)
    if not isinstance(value, str) or not value.strip():
        raise ToolError("validation", f"{field!r} must be a non-empty date string.",
                        hint="Use 'today', '+Nd', YYYY-MM-DD, or an ISO datetime.")
    v = value.strip()
    midnight = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    if v.lower() == "today":
        return midnight
    m = _REL_RE.match(v)
    if m:
        return midnight + timedelta(days=int(m.group(1)))
    try:
        dt = datetime.fromisoformat(v)  # date-only parses to midnight
    except ValueError:
        raise ToolError(
            "validation", f"{field!r}: cannot parse {value!r} as a date.",
            hint="Use 'today', '+Nd' (e.g. '+7d'), YYYY-MM-DD, or an ISO datetime.",
        ) from None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=tz)


def _ceil_to_grid(dt: datetime, step_minutes: int = GRID_MINUTES) -> datetime:
    floored = dt.replace(minute=(dt.minute // step_minutes) * step_minutes,
                         second=0, microsecond=0)
    return floored if floored >= dt else floored + timedelta(minutes=step_minutes)


# ------------------------------------------------- slot synthesis (pure)


def merge_busy_and_find_slots(
    busy_by_attendee: Dict[str, List[Tuple[datetime, datetime]]],
    window_start: datetime,
    window_end: datetime,
    duration: int,
    working_hours: Optional[Tuple[dtime, dtime]] = None,
) -> List[Tuple[datetime, datetime]]:
    """Pure slot synthesis: candidate starts on a wall-clock 30-minute
    grid inside [window_start, window_end); a slot of ``duration``
    minutes qualifies when NO attendee has an overlapping busy block
    (touching endpoints do not overlap) and, when ``working_hours``
    (start_time, end_time) is given, the whole slot falls inside those
    hours of a single day."""
    dur = timedelta(minutes=int(duration))
    if dur <= timedelta(0) or window_end <= window_start:
        return []
    step = timedelta(minutes=GRID_MINUTES)
    slots: List[Tuple[datetime, datetime]] = []
    t = _ceil_to_grid(window_start)
    while t + dur <= window_end:
        slot_end = t + dur
        if working_hours is not None:
            wh_start, wh_end = working_hours
            # end.time() > start.time() also rejects slots crossing midnight
            if not (wh_start <= t.time() < slot_end.time() <= wh_end):
                t += step
                continue
        if all(
            not any(bs < slot_end and be > t for bs, be in blocks)
            for blocks in busy_by_attendee.values()
        ):
            slots.append((t, slot_end))
        t += step
    return slots


# -------------------------------------------------------------- handlers


async def _list_events(ctx: Context, start: str = "today", end: str = "+7d",
                       offset: int = 0, limit: int = 25) -> Dict[str, Any]:
    tz = ctx.settings.ews_tz
    start_dt = _parse_when(start, "start", tz)
    end_dt = _parse_when(end, "end", tz)
    if end_dt <= start_dt:
        raise ToolError("validation", "'end' must be after 'start'.",
                        hint="Example: start='today', end='+7d'.")

    def work(account: Any) -> List[Any]:
        # .view() implements CalendarView: it EXPANDS recurring events into
        # occurrences overlapping the window. .filter() would return only
        # recurrence masters — wrong for a calendar listing. Never swap.
        return list(account.calendar.view(start=start_dt, end=end_dt))

    items = await ctx.gateway.call(work)
    total = len(items)  # cheaply known: the view is already materialized
    offset = max(0, int(offset))
    page = items[offset:offset + max(0, int(limit))]
    cards = [event_card(item, ctx.aliaser, tz) for item in page]
    return envelope(cards, total, offset)


def _attendee_entries(item: Any) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for att in [*(getattr(item, "required_attendees", None) or []),
                *(getattr(item, "optional_attendees", None) or [])]:
        mailbox = getattr(att, "mailbox", None)
        entry: Dict[str, Any] = {
            "email": getattr(mailbox, "email_address", None) or "",
            "response": str(getattr(att, "response_type", None) or "Unknown"),
        }
        name = getattr(mailbox, "name", None)
        if name and name != entry["email"]:
            entry["name"] = name
        out.append(entry)
    return out


async def _get_event(ctx: Context, id: str) -> Dict[str, Any]:
    tz = ctx.settings.ews_tz

    def work(account: Any) -> Any:
        results = list(account.fetch(ids=[(id, None)]))
        item = results[0] if results else None
        if isinstance(item, Exception):
            raise item  # dispatcher maps ErrorItemNotFound → not_found + hint
        if item is None:
            raise ToolError("not_found", f"No event found for id {id!r}.",
                            hint="Re-run list_events and use a fresh event id.")
        return item

    item = await ctx.gateway.call(work)
    event = event_card(item, ctx.aliaser, tz)
    event["attendees"] = _attendee_entries(item)
    source = getattr(item, "text_body", None) or ""
    if not source:
        body_obj = getattr(item, "body", None)
        if body_obj is not None:
            source = html_to_text(str(body_obj))
    event["body"] = clean_body(source, max_chars=2000)["text"]
    event["recurring"] = bool(getattr(item, "is_recurring", False)
                              or getattr(item, "recurrence", None))
    my_response = getattr(item, "my_response_type", None)
    if my_response:
        event["my_response"] = str(my_response)
    return {"ok": True, "event": event}


async def _check_availability(ctx: Context, attendees: List[str], start: str,
                              end: str, duration_minutes: int = 30,
                              working_hours_only: bool = True) -> Dict[str, Any]:
    tz = ctx.settings.ews_tz
    emails = [a.strip() for a in (attendees or []) if isinstance(a, str) and a.strip()]
    if not emails:
        raise ToolError("validation", "'attendees' must list at least one email.")
    if int(duration_minutes) <= 0:
        raise ToolError("validation", "'duration_minutes' must be positive.")
    start_dt = _parse_when(start, "start", tz)
    end_dt = _parse_when(end, "end", tz)
    if end_dt <= start_dt:
        raise ToolError("validation", "'end' must be after 'start'.")
    requests = [(email, "Required", False) for email in emails]

    def work(account: Any) -> List[Any]:
        return list(account.protocol.get_free_busy_info(
            accounts=requests, start=start_dt, end=end_dt))

    views = await ctx.gateway.call(work)
    per_attendee: Dict[str, List[Dict[str, Any]]] = {}
    busy_by_attendee: Dict[str, List[Tuple[datetime, datetime]]] = {}
    for email, view in zip(emails, views):
        if isinstance(view, Exception):
            raise view
        entries: List[Dict[str, Any]] = []
        blocks: List[Tuple[datetime, datetime]] = []
        for ev in getattr(view, "calendar_events", None) or []:
            ev_start = getattr(ev, "start", None)
            ev_end = getattr(ev, "end", None)
            if ev_start is None or ev_end is None:
                continue
            status = str(getattr(ev, "busy_type", None) or "Busy")
            entries.append({"start": fmt_dt(ev_start, tz),
                            "end": fmt_dt(ev_end, tz), "status": status})
            if status.lower() != "free":
                blocks.append((ev_start, ev_end))
        per_attendee[email] = entries
        busy_by_attendee[email] = blocks
    hours = WORKING_HOURS if working_hours_only else None
    raw = merge_busy_and_find_slots(busy_by_attendee, start_dt, end_dt,
                                    int(duration_minutes), hours)
    slots = [{"start": fmt_dt(s, tz), "end": fmt_dt(e, tz)} for s, e in raw]
    return {"ok": True, "slots": slots, "per_attendee": per_attendee}


def _gal_person(entry: Any) -> Optional[Tuple[str, Dict[str, Any]]]:
    """(raw_alias_key, person) — GAL has no stable item id, so the email
    itself is the raw key the 'p' alias binds to."""
    mailbox, contact = entry if isinstance(entry, tuple) else (entry, None)
    email = (getattr(mailbox, "email_address", None) or "").strip()
    if not email:
        return None
    person: Dict[str, Any] = {
        "name": getattr(mailbox, "name", None)
        or getattr(contact, "display_name", None) or email,
        "email": email,
        "source": "gal",
    }
    _enrich_from_contact(person, contact)
    return email, person


def _enrich_from_contact(person: Dict[str, Any], contact: Any) -> None:
    if contact is None:
        return
    title = getattr(contact, "job_title", None)
    if title:
        person["title"] = str(title)
    company = getattr(contact, "company_name", None)
    if company:
        person["company"] = str(company)
    for ph in getattr(contact, "phone_numbers", None) or []:
        number = getattr(ph, "phone_number", None)
        if number:
            person["phone"] = str(number)
            break


def _contact_person(contact: Any) -> Optional[Tuple[str, Dict[str, Any]]]:
    emails = [getattr(e, "email", None)
              for e in (getattr(contact, "email_addresses", None) or [])]
    email = next((e for e in emails if e), None)
    if not email:
        return None  # address-less contacts are useless to the secretary
    raw_key = str(getattr(contact, "id", None) or email)
    person: Dict[str, Any] = {
        "name": getattr(contact, "display_name", None) or email,
        "email": email,
        "source": "contacts",
    }
    _enrich_from_contact(person, contact)
    return raw_key, person


async def _find_people(ctx: Context, query: str, source: str = "auto",
                       limit: int = 10) -> Dict[str, Any]:
    q = (query or "").strip()
    if not q:
        raise ToolError("validation", "'query' must be a non-empty string.")
    ql = q.lower()

    def work(account: Any) -> Tuple[List[Any], List[Any]]:
        gal: List[Any] = []
        matched: List[Any] = []
        if source in ("auto", "gal"):
            gal = list(account.protocol.resolve_names(
                [q], return_full_contact_data=True))
        if source in ("auto", "contacts"):
            for contact in account.contacts.all():
                name = str(getattr(contact, "display_name", "") or "")
                emails = [str(getattr(e, "email", "") or "") for e in
                          (getattr(contact, "email_addresses", None) or [])]
                if ql in " ".join([name, *emails]).lower():
                    matched.append(contact)
        return gal, matched

    gal_entries, contact_items = await ctx.gateway.call(work)
    candidates: List[Tuple[str, Dict[str, Any]]] = []
    for entry in gal_entries:
        if isinstance(entry, Exception):  # ErrorNameResolutionNoResults etc.
            continue
        built = _gal_person(entry)
        if built:
            candidates.append(built)  # GAL first: wins duplicates below
    for contact in contact_items:
        built = _contact_person(contact)
        if built:
            candidates.append(built)
    people: List[Dict[str, Any]] = []
    seen: set = set()
    for raw_key, person in candidates:
        key = person["email"].lower()
        if key in seen:
            continue
        seen.add(key)
        # Mint aliases only for survivors so dropped duplicates never
        # consume p-numbers.
        people.append({"id": ctx.aliaser.alias_for(raw_key, "p"), **person})
    total = len(people)
    return envelope(people[:max(0, int(limit))], total, 0)


async def _get_oof_settings(ctx: Context) -> Dict[str, Any]:
    tz = ctx.settings.ews_tz
    oof = await ctx.gateway.call(lambda account: account.oof_settings)

    def reply_text(value: Any) -> str:
        return html_to_text(str(value))[:500] if value else ""

    out: Dict[str, Any] = {
        "ok": True,
        "state": str(getattr(oof, "state", None) or "Disabled").lower(),
        "internal_reply_text": reply_text(getattr(oof, "internal_reply", None)),
        "external_reply_text": reply_text(getattr(oof, "external_reply", None)),
    }
    for key in ("start", "end"):
        formatted = fmt_dt(getattr(oof, key, None), tz)
        if formatted:
            out[key] = formatted
    return out


async def _get_server_status(ctx: Context) -> Dict[str, Any]:
    # requires_ews=False: answers even while Exchange is cold. No network.
    return {
        "ok": True,
        "version": __version__,
        "uptime_s": int(time.time() - ctx.started_at),
        "connection": ctx.manager.status() if ctx.manager else {"state": "unmanaged"},
        "tier": ctx.settings.ews_capability_tier,
        "send_kill_switch": not ctx.settings.send_enabled,
        "tools": len(ctx.registry),
        "counters": dict(ctx.counters),
        "alias_stats": ctx.aliaser.stats(),
    }


# ---------------------------------------------------------------- specs

_DATE_DESC = "'today', '+Nd' (relative days), YYYY-MM-DD, or ISO datetime"

TOOLS: List[ToolSpec] = [
    ToolSpec(
        name="list_events",
        description=(
            "List calendar events overlapping a time window (default: today "
            "through +7d, server timezone). Recurring meetings are expanded "
            "into individual occurrences — a weekly meeting shows once per "
            "occurrence in the window. Returns a paged envelope of compact "
            "event cards; their ids are short aliases (e1, e2, …) you pass "
            "to get_event."
        ),
        side_effect_class="read",
        input_schema={
            "type": "object",
            "properties": {
                "start": {"type": "string", "default": "today",
                          "description": f"Window start: {_DATE_DESC}."},
                "end": {"type": "string", "default": "+7d",
                        "description": f"Window end (exclusive): {_DATE_DESC}."},
                "offset": {"type": "integer", "default": 0, "minimum": 0,
                           "description": "Pagination offset into the window."},
                "limit": {"type": "integer", "default": 25, "minimum": 1,
                          "maximum": 100, "description": "Max events returned."},
            },
            "required": [],
            "additionalProperties": False,
        },
        handler=_list_events,
    ),
    ToolSpec(
        name="get_event",
        description=(
            "Fetch one calendar event in full by id (a short alias like 'e3' "
            "from list_events, or a raw EWS id). Returns organizer, every "
            "attendee with their response (Accept/Decline/Tentative/…), the "
            "cleaned body text, a recurring flag, and your own response."
        ),
        side_effect_class="read",
        input_schema={
            "type": "object",
            "properties": {
                "id": {"type": "string",
                       "description": "Event id — alias (e1, e2, …) or raw EWS id."},
            },
            "required": ["id"],
            "additionalProperties": False,
        },
        handler=_get_event,
    ),
    ToolSpec(
        name="check_availability",
        description=(
            "Check free/busy for one or more attendees and propose meeting "
            "slots. Each returned slot is a candidate start/end (30-minute "
            "grid) of duration_minutes where EVERY listed attendee is free; "
            "per_attendee shows the raw busy blocks behind the answer. With "
            "working_hours_only (default true) slots are confined to "
            "09:00-17:00 in the server timezone. An empty slots list means "
            "no common free window exists in the range."
        ),
        side_effect_class="read",
        input_schema={
            "type": "object",
            "properties": {
                "attendees": {
                    "type": "array", "items": {"type": "string"}, "minItems": 1,
                    "description": "SMTP email addresses to check.",
                },
                "start": {"type": "string",
                          "description": f"Window start: {_DATE_DESC}."},
                "end": {"type": "string",
                        "description": f"Window end: {_DATE_DESC}."},
                "duration_minutes": {"type": "integer", "default": 30,
                                     "minimum": 1, "maximum": 1440,
                                     "description": "Desired meeting length."},
                "working_hours_only": {"type": "boolean", "default": True,
                                       "description": "Confine slots to 09:00-17:00."},
            },
            "required": ["attendees", "start", "end"],
            "additionalProperties": False,
        },
        handler=_check_availability,
    ),
    ToolSpec(
        name="find_people",
        description=(
            "Find people by name or email fragment in the GAL (global "
            "address list) and/or the personal contacts folder. "
            "source='auto' merges both, deduplicating by email with the GAL "
            "entry winning. Returned ids are short aliases (p1, p2, …)."
        ),
        side_effect_class="read",
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string",
                          "description": "Name or email fragment, e.g. 'ahmed'."},
                "source": {"type": "string", "enum": ["auto", "gal", "contacts"],
                           "default": "auto",
                           "description": "Where to search; auto = GAL + contacts."},
                "limit": {"type": "integer", "default": 10, "minimum": 1,
                          "maximum": 50, "description": "Max people returned."},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        handler=_find_people,
    ),
    ToolSpec(
        name="get_oof_settings",
        description=(
            "Read the mailbox out-of-office (automatic reply) settings: "
            "state (disabled/enabled/scheduled), the scheduled window when "
            "applicable, and the internal/external reply texts (HTML "
            "stripped, capped at 500 chars)."
        ),
        side_effect_class="read",
        input_schema={
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        },
        handler=_get_oof_settings,
    ),
    ToolSpec(
        name="get_server_status",
        description=(
            "This server's own status: version, uptime, Exchange connection "
            "state, capability tier, send kill-switch, tool count, per-tool "
            "call counters and alias statistics. Never touches Exchange — "
            "it answers even while the connection is still warming up, so "
            "use it to explain connectivity problems."
        ),
        side_effect_class="read",
        input_schema={
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        },
        handler=_get_server_status,
        requires_ews=False,
    ),
]
