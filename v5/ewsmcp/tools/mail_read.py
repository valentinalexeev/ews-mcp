"""Tool pack: mail_read — the six read-class tools of IDEATION §5 (1-6).

Handlers receive RAW EWS ids (the dispatcher already resolved m##/t##
aliases on the way in) and emit DTOs whose ids are short aliases. No
safety logic lives here — the dispatcher owns tier / kill-switch /
confirm gates. No direct exchangelib import: all EWS work goes through
``account`` attributes inside closures run on the gateway pool.
"""

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from ..bodyclean import clean_body
from ..dto import envelope, event_card, fmt_dt, msg_card, msg_full
from ..errors import ToolError
from ..gateway.client import WELL_KNOWN, paginate
from .base import Context, ToolSpec

# Fields fetched for every list/search projection (token economy: never
# pull full MIME just to render a card).
_PROJECTION = (
    "id", "subject", "sender", "datetime_received", "is_read",
    "has_attachments", "text_body", "to_recipients", "conversation_id",
    "message_id", "importance", "attachments",
)

_TEXT_EXTS = (".txt", ".csv", ".md", ".log", ".json")
_TEXT_CAP = 20_000


# --------------------------------------------------------------------------
# Shared sync helpers (called inside gateway lambdas, on the EWS pool)
# --------------------------------------------------------------------------


def _project(qs: Any, order: Optional[str] = "-datetime_received") -> Any:
    """Best-effort .order_by()/.only() — some folder types reject them."""
    if order:
        try:
            qs = qs.order_by(order)
        except Exception:
            pass
    try:
        qs = qs.only(*_PROJECTION)
    except Exception:
        pass
    return qs


def _fetch_one(account: Any, raw_id: str, only: Optional[List[str]] = None) -> Any:
    """Fetch a single item by raw EWS id via account.fetch.

    account.fetch yields per-item results; a missing/stale id arrives as an
    Exception INSTANCE in the stream — re-raise it so the dispatcher's error
    mapper classifies it (ErrorItemNotFound → not_found + re-search hint).
    """
    results = list(account.fetch(ids=[(raw_id, None)], only_fields=only))
    if not results:
        raise ToolError(
            "not_found", "Message not found — the id may be stale.",
            hint="Ids change when items move; re-run search_messages for a fresh id.",
        )
    first = results[0]
    if isinstance(first, Exception):
        raise first
    return first


def _parse_when(value: str, tz: str) -> datetime:
    """ISO-8601 → tz-aware datetime; date-only means midnight in EWS_TZ."""
    try:
        dt = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        raise ToolError(
            "validation",
            f"Cannot parse date {value!r}.",
            hint="Use ISO-8601: '2026-06-01' or '2026-06-01T09:30+03:00'.",
        )
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo(tz))
    return dt


def _from(item: Any) -> str:
    sender = getattr(item, "sender", None)
    name = getattr(sender, "name", None)
    email = getattr(sender, "email_address", None) or ""
    return f"{name} <{email}>" if name and name != email else email


def _pick_attachment(atts: List[Any], selector: Optional[str]) -> Any:
    names = [getattr(a, "name", "") or f"attachment-{i}" for i, a in enumerate(atts)]
    if selector is None:
        if len(atts) == 1:
            return atts[0]
        raise ToolError(
            "validation",
            f"The message has {len(atts)} attachments — pick one: {', '.join(names)}.",
            hint="Pass attachment=<name> or attachment=<zero-based index as a string>.",
        )
    sel = str(selector).strip()
    for att, name in zip(atts, names):
        if name.lower() == sel.lower():
            return att
    digits = sel[1:] if sel[:1] == "a" and sel[1:].isdigit() else sel
    if digits.isdigit() and int(digits) < len(atts):
        return atts[int(digits)]
    raise ToolError(
        "not_found",
        f"No attachment matches {selector!r}. Available: {', '.join(names)}.",
    )


def _is_texty(name: str, content_type: Optional[str]) -> bool:
    return ((content_type or "").lower().startswith("text/")
            or (name or "").lower().endswith(_TEXT_EXTS))


# --------------------------------------------------------------------------
# 1. list_folders
# --------------------------------------------------------------------------


async def _list_folders(ctx: Context, parent: Optional[str] = None, depth: int = 2,
                        include_empty: bool = True) -> Dict[str, Any]:
    depth = max(1, min(int(depth), 5))

    def work(account: Any) -> List[Dict[str, Any]]:
        root = (ctx.gateway.resolve_folder(account, parent, ctx.aliaser)
                if parent else account.msg_folder_root)
        wk_by_raw_id: Dict[Any, str] = {}
        for wk_alias, attr in WELL_KNOWN.items():
            try:  # best effort — wk tagging must never break the walk
                fid = getattr(getattr(account, attr, None), "id", None)
                if fid:
                    wk_by_raw_id.setdefault(fid, wk_alias)
            except Exception:
                continue
        rows: List[Dict[str, Any]] = []

        def walk(folder: Any, level: int, prefix: str) -> None:
            if level > depth:
                return
            for child in list(getattr(folder, "children", None) or []):
                name = getattr(child, "name", "") or ""
                path = f"{prefix}/{name}" if prefix else name
                total = getattr(child, "total_count", None) or 0
                raw_id = getattr(child, "id", None)
                grandchildren = list(getattr(child, "children", None) or [])
                if include_empty or total:
                    row: Dict[str, Any] = {
                        "id": ctx.aliaser.alias_for(str(raw_id), "f") if raw_id else None,
                        "name": name,
                        "path": path,
                        "total": total,
                        "unread": getattr(child, "unread_count", None) or 0,
                        "children": len(grandchildren),
                    }
                    wk = wk_by_raw_id.get(raw_id)
                    if wk:
                        row["wk"] = wk
                    rows.append(row)
                walk(child, level + 1, path)

        walk(root, 1, "")
        return rows

    rows = await ctx.gateway.call(work)
    return envelope(rows, total_available=len(rows), offset=0)


# --------------------------------------------------------------------------
# 2. search_messages
# --------------------------------------------------------------------------


async def _search_messages(ctx: Context, query: Optional[str] = None,
                           folder: str = "f:inbox", from_: Optional[str] = None,
                           subject: Optional[str] = None, since: Optional[str] = None,
                           until: Optional[str] = None, is_unread: Optional[bool] = None,
                           has_attachments: Optional[bool] = None,
                           offset: int = 0, limit: int = 20) -> Dict[str, Any]:
    offset = max(0, int(offset))
    limit = max(1, min(int(limit), 50))
    structured_given = any(
        v is not None for v in (from_, subject, since, until, is_unread, has_attachments)
    )
    if query and structured_given:
        raise ToolError(
            "validation",
            "`query` (AQS) cannot be combined with the structured filters "
            "(from_/subject/since/until/is_unread/has_attachments) — Exchange "
            "runs them on different engines.",
            hint="Either fold everything into the AQS string "
                 "(e.g. 'from:ahmed subject:rfp received>=2026-06-01') or drop "
                 "`query` and use only structured filters.",
        )
    tz = ctx.settings.ews_tz
    filters: Dict[str, Any] = {}
    if subject:
        filters["subject__icontains"] = subject
    if since:
        filters["datetime_received__gte"] = _parse_when(since, tz)
    if until:
        filters["datetime_received__lte"] = _parse_when(until, tz)
    if is_unread is not None:
        filters["is_read"] = not is_unread
    if has_attachments is not None:
        filters["has_attachments"] = bool(has_attachments)

    def work(account: Any) -> Tuple[List[Any], Optional[int]]:
        target = ctx.gateway.resolve_folder(account, folder, ctx.aliaser)
        qs = target.filter(query) if query else target.filter(**filters)
        return paginate(_project(qs), offset=offset, limit=limit)

    items, total = await ctx.gateway.call(work)
    if from_:
        needle = from_.strip().lower()

        def hit(it: Any) -> bool:
            sender = getattr(it, "sender", None)
            email = (getattr(sender, "email_address", "") or "").lower()
            name = (getattr(sender, "name", "") or "").lower()
            return needle in email or needle in name

        items = [it for it in items if hit(it)]
        total = None  # client-side post-filter: the upstream count no longer applies
    cards = [msg_card(it, ctx.aliaser, tz) for it in items]
    return envelope(cards, total_available=total, offset=offset)


# --------------------------------------------------------------------------
# 3. get_message
# --------------------------------------------------------------------------


async def _get_message(ctx: Context, id: str, format: str = "full",
                       include_html: bool = False) -> Dict[str, Any]:
    raw_id = id
    item = await ctx.gateway.call(lambda account: _fetch_one(account, raw_id))
    tz = ctx.settings.ews_tz
    if format == "concise":
        return {"ok": True, "message": msg_card(item, ctx.aliaser, tz)}
    return {"ok": True, "message": msg_full(
        item, ctx.aliaser, tz, ctx.settings.body_max_chars,
        include_html=bool(include_html),
    )}


# --------------------------------------------------------------------------
# 4. get_thread
# --------------------------------------------------------------------------


async def _get_thread(ctx: Context, id: str, limit: int = 20) -> Dict[str, Any]:
    limit = max(1, min(int(limit), 50))
    raw_id = id
    tz = ctx.settings.ews_tz

    def work(account: Any) -> Tuple[Any, str, List[Any]]:
        seed = _fetch_one(account, raw_id)
        conv_id = getattr(getattr(seed, "conversation_id", None), "id", None)
        if not conv_id:
            raise ToolError(
                "not_found",
                "That message carries no conversation id; cannot rebuild a thread.",
                hint="Use get_message on it instead.",
            )
        # 5.0.3: filter wants the conversation id STRING (the object raises).
        conv_str = str(conv_id)
        merged: Dict[str, Any] = {}
        for source in (account.inbox, account.sent):
            qs = _project(source.filter(conversation_id=conv_str), order=None)
            for it in qs:
                key = str(getattr(it, "id", "") or "")
                if key:
                    merged.setdefault(key, it)
        if not merged:  # seed lives elsewhere (archive…) — show at least it
            merged[str(getattr(seed, "id", "") or raw_id)] = seed
        return seed, conv_str, list(merged.values())

    seed, conv_str, found = await ctx.gateway.call(work)

    def sort_key(it: Any) -> datetime:
        dt = getattr(it, "datetime_received", None)
        return dt if dt is not None else datetime.min.replace(tzinfo=timezone.utc)

    found.sort(key=sort_key)
    total = len(found)
    if total > limit:
        found = found[-limit:]  # chronological, most recent `limit` entries
    counts: Dict[str, int] = {}
    entries: List[Dict[str, Any]] = []
    for it in found:
        sender = _from(it) or "unknown"
        counts[sender] = counts.get(sender, 0) + 1
        raw = str(getattr(it, "id", "") or "")
        entry: Dict[str, Any] = {
            "id": ctx.aliaser.alias_for(
                raw, "m", internet_message_id=getattr(it, "message_id", None),
            ) if raw else None,
            "from": sender,
            "date": fmt_dt(getattr(it, "datetime_received", None), tz),
            "body": clean_body(getattr(it, "text_body", None) or "",
                               max_chars=1500)["text"],
        }
        if getattr(it, "has_attachments", False):
            entry["attach"] = True
        entries.append(entry)
    participants = [
        {"name_or_email": who, "msgs": n}
        for who, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    ]
    return {
        "ok": True,
        "thread_id": ctx.aliaser.alias_for(conv_str, "t"),
        "subject": getattr(seed, "subject", "") or "",
        "participants": participants,
        "items": entries,
        "count": len(entries),
        "total_available": total,
    }


# --------------------------------------------------------------------------
# 5. get_attachment
# --------------------------------------------------------------------------


async def _get_attachment(ctx: Context, message_id: str,
                          attachment: Optional[str] = None,
                          mode: str = "auto") -> Dict[str, Any]:
    raw_id = message_id

    def work(account: Any) -> Dict[str, Any]:
        item = _fetch_one(account, raw_id, only=["attachments"])
        atts = list(getattr(item, "attachments", None) or [])
        if not atts:
            raise ToolError("not_found", "This message has no attachments.")
        att = _pick_attachment(atts, attachment)
        name = getattr(att, "name", "") or "attachment"
        content_type = getattr(att, "content_type", None)
        out: Dict[str, Any] = {
            "ok": True,
            "name": name,
            "size_bytes": getattr(att, "size", None),
            "content_type": content_type,
        }
        chosen = mode
        if mode == "auto":
            chosen = "text" if _is_texty(name, content_type) else "info"
            if chosen == "info":
                out["hint"] = ("Binary attachment — metadata only. Call again "
                               "with mode='save' to write it to disk.")
        if chosen == "info":
            out["mode"] = "info"
            return out
        content = getattr(att, "content", None)  # FileAttachment bytes (5.0.3)
        if not isinstance(content, (bytes, bytearray)):
            raise ToolError(
                "validation",
                f"Attachment {name!r} has no retrievable bytes (item attachment?).",
                hint="Only file attachments can be read; use mode='info'.",
            )
        data = bytes(content)
        if chosen == "text":
            text = data.decode("utf-8", errors="replace")
            out["mode"] = "text"
            out["text"] = text[:_TEXT_CAP]
            if len(text) > _TEXT_CAP:
                out["truncated"] = True
            return out
        safe = re.sub(r"[^\w.\-]+", "_", name).strip("._") or "attachment.bin"
        dest = Path(ctx.settings.data_dir) / "attachments"
        dest.mkdir(parents=True, exist_ok=True)
        path = dest / safe
        path.write_bytes(data)
        out["mode"] = "save"
        out["saved_path"] = str(path)
        return out

    return await ctx.gateway.call(work)


# --------------------------------------------------------------------------
# 6. get_mailbox_overview
# --------------------------------------------------------------------------


async def _get_mailbox_overview(ctx: Context, horizon_days: int = 1) -> Dict[str, Any]:
    horizon_days = max(1, min(int(horizon_days), 14))
    tz = ctx.settings.ews_tz
    now = datetime.now(ZoneInfo(tz))
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + timedelta(days=horizon_days)

    def work(account: Any) -> Tuple[int, List[Any], List[Any]]:
        unread_total = getattr(account.inbox, "unread_count", None) or 0
        qs = _project(account.inbox.filter(is_read=False))
        recent, _total = paginate(qs, offset=0, limit=10)
        # .view() (not .filter()) so recurring events are expanded.
        events = list(account.calendar.view(start=day_start, end=day_end))[:10]
        return unread_total, recent, events

    unread_total, recent, events = await ctx.gateway.call(work)
    return {
        "ok": True,
        "generated_at": now.isoformat(timespec="seconds"),
        "unread_total": unread_total,
        "recent_unread": [msg_card(it, ctx.aliaser, tz) for it in recent],
        "today_events": [event_card(ev, ctx.aliaser, tz) for ev in events],
        "connection": ctx.manager.state if ctx.manager else "unmanaged",
    }


# --------------------------------------------------------------------------
# Specs
# --------------------------------------------------------------------------


def _schema(properties: Dict[str, Any],
            required: Optional[List[str]] = None) -> Dict[str, Any]:
    schema: Dict[str, Any] = {
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
    }
    if required:
        schema["required"] = required
    return schema


TOOLS: List[ToolSpec] = [
    ToolSpec(
        name="list_folders",
        description=(
            "List mail folders as a depth-limited tree walk. Each row is "
            "{id, name, path, total, unread, children}; `id` is a short folder "
            "alias (f7) reusable as a `folder`/`parent` argument anywhere. "
            "Well-known folders also carry `wk` (e.g. 'f:inbox') — prefer "
            "passing that stable alias. Set include_empty=false to hide "
            "folders with zero items."
        ),
        side_effect_class="read",
        requires_ews=True,
        input_schema=_schema({
            "parent": {
                "type": "string",
                "description": "Start folder: well-known alias (f:inbox), folder "
                               "alias (f7), path, or raw id. Default: the "
                               "message-folder root.",
            },
            "depth": {"type": "integer", "minimum": 1, "maximum": 5, "default": 2},
            "include_empty": {"type": "boolean", "default": True},
        }),
        handler=_list_folders,
    ),
    ToolSpec(
        name="search_messages",
        description=(
            "Search mail. TWO ENGINES, mutually exclusive: pass `query` (an "
            "Exchange AQS string, e.g. 'from:ahmed subject:rfp hasattachment:yes') "
            "OR the structured filters (subject/since/until/is_unread/"
            "has_attachments) — combining `query` with any structured filter is "
            "a validation error. `from_` is matched client-side against the "
            "fetched page's sender email/name, so total_available is unknown "
            "when it is used. Results are compact cards, newest first; their "
            "`id` values are short aliases (m12) for get_message / get_thread / "
            "get_attachment. If an id later goes stale (items move), re-run "
            "this search for fresh ids."
        ),
        side_effect_class="read",
        requires_ews=True,
        input_schema=_schema({
            "query": {
                "type": "string",
                "description": "AQS query string — cannot be combined with the "
                               "structured filters below.",
            },
            "folder": {
                "type": "string", "default": "f:inbox",
                "description": "Folder alias (f:inbox, f:sent, f7), path, or raw id.",
            },
            "from_": {
                "type": "string",
                "description": "Sender substring, matched client-side on the "
                               "fetched page (email or display name).",
            },
            "subject": {"type": "string", "description": "Subject substring."},
            "since": {
                "type": "string",
                "description": "ISO date/datetime; date-only means midnight "
                               "(server timezone).",
            },
            "until": {
                "type": "string",
                "description": "ISO date/datetime; date-only means midnight "
                               "(server timezone).",
            },
            "is_unread": {"type": "boolean"},
            "has_attachments": {"type": "boolean"},
            "offset": {"type": "integer", "minimum": 0, "default": 0},
            "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 20},
        }),
        handler=_search_messages,
    ),
    ToolSpec(
        name="get_message",
        description=(
            "Fetch one message by id (short alias from search results, e.g. "
            "m12; raw EWS ids also work). format='full' (default) returns "
            "recipients, the cleaned body (quoted history stripped — use "
            "get_thread for the conversation) and the attachment inventory; "
            "format='concise' returns the compact card. A not_found means the "
            "id went stale (items move) — re-run search_messages and retry "
            "with a fresh id."
        ),
        side_effect_class="read",
        requires_ews=True,
        input_schema=_schema({
            "id": {"type": "string", "description": "Message id (m-alias or raw)."},
            "format": {"type": "string", "enum": ["full", "concise"],
                       "default": "full"},
            "include_html": {
                "type": "boolean", "default": False,
                "description": "Also return the raw HTML body (rendering edge "
                               "cases only — it is token-expensive).",
            },
        }, required=["id"]),
        handler=_get_message,
    ),
    ToolSpec(
        name="get_thread",
        description=(
            "Rebuild the conversation containing the given message id: Inbox "
            "and Sent are merged and sorted chronologically, each entry's body "
            "cleaned to its latest-reply-only text. Returns thread_id "
            "(t-alias), participants with message counts, and the most recent "
            "`limit` entries. Entry ids are m-aliases usable with get_message/"
            "get_attachment. Stale id → re-run search_messages."
        ),
        side_effect_class="read",
        requires_ews=True,
        input_schema=_schema({
            "id": {"type": "string",
                   "description": "Any message id in the thread (m-alias or raw)."},
            "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 20},
        }, required=["id"]),
        handler=_get_thread,
    ),
    ToolSpec(
        name="get_attachment",
        description=(
            "Read one attachment of a message. mode='info' → metadata only; "
            "'text' → decoded UTF-8 text (capped at 20000 chars, `truncated` "
            "flagged) for text-like attachments (text/* content type or "
            ".txt/.csv/.md/.log/.json name); 'save' → write the bytes under "
            "the server data dir and return saved_path; 'auto' (default) → "
            "text when text-like, otherwise info plus a hint. When the message "
            "has several attachments you MUST pick one via `attachment` (a "
            "name, or a zero-based index as a string)."
        ),
        side_effect_class="read",
        requires_ews=True,
        input_schema=_schema({
            "message_id": {"type": "string",
                           "description": "Message id (m-alias or raw)."},
            "attachment": {
                "type": "string",
                "description": "Attachment name or zero-based index as a string "
                               "(e.g. '0'). Optional only when the message has "
                               "exactly one attachment.",
            },
            "mode": {"type": "string", "enum": ["auto", "info", "text", "save"],
                     "default": "auto"},
        }, required=["message_id"]),
        handler=_get_attachment,
    ),
    ToolSpec(
        name="get_mailbox_overview",
        description=(
            "The morning-brief workflow tool — start here. One call returns "
            "unread_total, the 10 most recent unread message cards, today's "
            "calendar events (recurrences expanded) across `horizon_days` "
            "days, and the Exchange connection state. Card ids are short "
            "aliases (m12 / e3) for the other tools."
        ),
        side_effect_class="read",
        requires_ews=True,
        input_schema=_schema({
            "horizon_days": {"type": "integer", "minimum": 1, "maximum": 14,
                             "default": 1},
        }),
        handler=_get_mailbox_overview,
    ),
]
