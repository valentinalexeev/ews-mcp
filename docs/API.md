# API Documentation

Complete reference for all EWS MCP Server tools: **42 base tools** (always available, subject to category flags) and **4 optional AI tools** (`semantic_search_emails`, `classify_email`, `summarize_email`, `suggest_replies`). Grand total: **46**.

| Category | Tools |
|----------|-------|
| Email | 10 |
| Email Drafts | 3 |
| Attachments | 7 |
| Calendar | 7 |
| Contacts | 3 |
| Contact Intelligence | 2 |
| Tasks | 5 |
| Search | 1 |
| Folders | 3 |
| Out-of-Office | 1 |
| AI (optional) | 4 |

Every base tool accepts `target_mailbox` (when `EWS_IMPERSONATION_ENABLED=true`). **AI tools do not**, and always act on the primary authenticated mailbox.

All responses follow the same shape:

```json
{ "success": true, "message": "...", "data": { ... } }
```

On failure:

```json
{ "success": false, "error": "short description (max 200 chars)" }
```

## Endpoints

With `MCP_TRANSPORT=sse` the server exposes one HTTP surface carrying both MCP transports plus a REST adapter. When `MCP_API_KEY` is set, every endpoint **except** `/health`, `/livez`, and `/readyz` requires `Authorization: Bearer <key>` (or `X-API-Key: <key>`).

| Endpoint | Method | Auth | Purpose |
|---|---|---|---|
| `/health` | GET | public | `{status, tools, sse_active_connections}` |
| `/livez` | GET | public | Liveness — `200 {"status":"ok"}` whenever the process/event loop is up |
| `/readyz` | GET | public | Readiness — `200` when the Exchange connection is warm, `503` otherwise (see below) |
| `/mcp` | POST/GET/DELETE | key | MCP **Streamable HTTP** transport (stateless) — for modern MCP clients |
| `/sse` | GET | key | Legacy MCP HTTP+SSE transport (paired with `/messages`) |
| `/messages` | POST | key | Message channel for the legacy SSE transport |
| `/api/tools/<name>` | POST | key | REST execution of any registered tool (JSON body = tool arguments) |
| `/openapi.json` | GET | key | OpenAPI schema for the REST surface |

### /livez and /readyz

Startup never exits on a failed Exchange connection: tools register and transports bind immediately while a background warmup loop (`src/connection_manager.py`) retries with exponential backoff + full jitter, then a periodic heartbeat re-probes the warm connection. `/livez` answers `200` as soon as the process is up; `/readyz` reports the connection state:

**200 (warm):**
```json
{
  "status": "ok",
  "connection": {
    "state": "warm",
    "attempts": 1,
    "last_error": null,
    "last_success_age_s": 12,
    "next_retry_in_s": null
  },
  "tools": 78
}
```

**503 (warming up or degraded):**
```json
{
  "status": "unavailable",
  "connection": {
    "state": "connecting",            // connecting | warm | degraded
    "attempts": 7,
    "last_error": "AutoDiscoverFailed: ...",
    "last_success_age_s": null,
    "next_retry_in_s": 42
  },
  "tools": 78
}
```

While the state is `connecting` (never connected since process start), EWS-touching tools fail fast with a retry hint instead of hanging; local tools and `whoami` (with `probe_connection=false`) keep working. `whoami` reports the same snapshot under `connection.managed`.

Related settings: `EWS_WARMUP_MAX_BACKOFF_SECONDS` (default 300), `EWS_HEARTBEAT_SECONDS` (600; 0 disables), `EWS_RETRY_MAX_WAIT_SECONDS` (300 — exchangelib `FaultTolerance` retry budget for `ErrorServerBusy`/transient 503s), `EWS_OFFLOAD_ENABLED` (true — run EWS tool bodies on a bounded worker pool instead of the event loop), `EWS_MAX_CONCURRENCY` (4 — pool size = max concurrent EWS operations).

## Two-phase confirmation

Irreversible or externally-visible operations are gated by a central two-phase confirm:

1. **Phase 1 — preview.** Call the tool **without** `confirm_token`. Nothing executes; the response is:

```json
{
  "success": true,
  "requires_confirmation": true,
  "message": "send_email previewed — NOTHING was executed. Review the preview, then call again with the same arguments plus confirm_token to proceed.",
  "preview": { "to": ["a@example.com"], "subject": "...", "body_preview": "first 200 chars..." },
  "confirm_token": "eyJhIjoi...{signature}",
  "expires_at": 1781000000,
  "ttl_seconds": 600,
  "warnings": ["external recipients: a@other-domain.com"]
}
```

(`warnings` appears only when applicable, e.g. recipients outside the primary mailbox's domain.)

2. **Phase 2 — execute.** Call again with **identical arguments** plus `confirm_token`. The token is a stateless HMAC bound to (mailbox, tool, exact arguments): changing any argument, replaying it on another tool, or exceeding the TTL (default 600 s) makes it verify as stale/expired, and the call fails with a clean validation error telling you to re-preview.

`confirm_token` is injected automatically into the published schema of every gated tool.

| Gating | Tools |
|---|---|
| Always | `send_email`, `reply_email`, `forward_email`, `respond_to_meeting`, `delete_appointment` |
| Conditional | `delete_email` and `delete_messages` (only when `permanent`/`hard_delete` is true), `manage_folder` (only `action: "delete"`), `oof_settings` (only `action: "set"`) |
| Own flow | `send_draft` — its token is bound to the draft's *content*, so editing the draft invalidates it (see its entry) |

Tokens are signed with `SEND_CONFIRM_SECRET` when set (so they survive restarts); otherwise a per-process secret is used. The approval-queue executor (`execute_approved_action`) pre-confirms through the same HMAC machinery — a human approval mints a valid phase-2 token rather than bypassing the gate.

### Capability tiers

`EWS_CAPABILITY_TIER=read|draft|full` (default `full` = historical behavior) caps which side-effect classes may run:

- `read` — read-only tools.
- `draft` — adds reversible writes (drafts, flags, moves, soft delete).
- `full` — adds send and destructive classes.

Above-tier tools are **removed from the registry at startup** and additionally **refused at dispatch** (defense in depth). Run `draft` against a production mailbox you must not send from.

### Recipient guards and rate cap (send-class tools)

- `EWS_RECIPIENT_ALLOWLIST` / `EWS_RECIPIENT_DENYLIST` — comma-separated `fnmatch` globs (e.g. `*@example.com`). When the allowlist is non-empty, any argument-visible recipient not matching it is refused; any recipient matching the denylist is refused (denylist wins). Limitation: only recipients visible in the call's arguments are checked — `send_draft` recipients live in the draft itself.
- `EWS_MAX_SENDS_PER_HOUR` — hourly cap across **all** send-class executions; `0` (default) = unlimited.
- `SEND_ENABLED=false` — global kill switch: every send-class tool refuses before any network call.

### Audit chain

Every `audit.log` record is hash-chained (`h = sha256(prev_hash | core)` with `seq`/`prev` fields), so an edited or deleted record breaks every hash after it. Verify with:

    python scripts/verify_audit_chain.py logs/audit.log

### Tool annotations

`list_tools` publishes MCP tool annotations derived from each tool's side-effect class: `read` → `readOnlyHint`/`idempotentHint`, `destructive` → `destructiveHint`, `send` → `destructiveHint` + `openWorldHint`. These are hints for clients; the server-side gates above remain the real boundary.

## Short ids (sid)

Raw EWS ItemIds are ~100–150 case-sensitive characters and silently change when an item moves folders. With `EWS_ID_ALIASES_ENABLED=true` (default), the server maintains short aliases:

- Every long EWS id in a tool output gains a sibling `"sid"` (`m12`, `e3`, `d4`, …); conversation/thread ids get `"thread_sid"` (`t1`, …).
- Kind letters: `m` message, `d` draft, `e` event/appointment, `k` task, `c` contact, `a` attachment, `t` conversation/thread, `f` folder.
- **Every id parameter accepts either an alias or a raw id.** Anything not alias-shaped (`^[a-z]{1,2}[0-9]+$`) passes through unchanged, so raw ids keep working everywhere.
- **Aliases survive moves.** When a move response reports `{old_id, new_id}` the alias is rebound to the new raw id — the handle you hold keeps working after `move_email` / `move_messages`.
- A stale or unknown alias returns a clean validation error ("re-run the search/list tool to get fresh ids"), not a 500.
- State persists in `<EWS_MEMORY_DIR>/aliases.db` (SQLite, WAL), so aliases survive restarts.

Aliasing is strictly additive: any aliasing failure degrades to raw-id passthrough and never takes a tool call down.

## Meta Tools

### whoami

Identity, backend, capabilities, and health self-report — the recommended first call. Read-only; works even while the background Exchange warmup is still connecting.

**Input Schema:**
```json
{
  "target_mailbox": "other@example.com",  // Optional: probe a delegated/impersonated mailbox
  "probe_connection": true                // Default true; false = pure-config answer, no network call
}
```

**Response (abridged):**
```json
{
  "success": true,
  "message": "EWS-MCP 3.5.0 — you@example.com",
  "mailbox": "you@example.com",
  "primary_mailbox": "you@example.com",
  "backend": "ews",
  "server": "https://mail.example.com/EWS/Exchange.asmx",
  "autodiscover": false,
  "auth_type": "ntlm",
  "impersonation": { "enabled": false, "type": "impersonation" },
  "transport": "sse",
  "send_enabled": true,
  "timezone": "Asia/Riyadh",
  "server_name": "ews-mcp-server",
  "version": "3.5.0",
  "tool_count": 78,
  "connection": {
    "probed": true,
    "managed": {
      "state": "warm",              // connecting | warm | degraded
      "attempts": 1,
      "last_error": null,
      "last_success_age_s": 12,
      "next_retry_in_s": null
    },
    "ok": true,
    "server_version": "Exchange2016",
    "api_version": "Exchange2016"
  },
  "ews_sunset": { "applies": false, "backend": "on_premises", "note": "..." }
}
```

`connection.managed` is the background warmup/heartbeat state (same snapshot as `/readyz`); `ews_sunset` surfaces the Exchange Online EWS retirement advisory when the backend is Exchange Online.

## Contact Intelligence Tools (v3.3 Consolidated)

These tools leverage the person-centric architecture with multi-strategy GAL search.

### find_person

Unified contact lookup across Global Address List (GAL), contacts, email history, and domains.

**v3.3 Changes:** Replaces `search_contacts`, `get_contacts`, and `resolve_names`. Use `source` param to select search scope.

**Input Schema:**
```json
{
  "query": "Ahmed",                    // Name, email, or @domain (optional if source=contacts)
  "source": "all",                     // all, gal, contacts, email_history, domain
  "include_stats": true,               // Include communication statistics
  "time_range_days": 365,              // Days back for email history
  "max_results": 50                    // Maximum results to return
}
```

**Response:**
```json
{
  "success": true,
  "message": "Found 5 contact(s)",
  "total_results": 5,
  "query": "Ahmed",
  "unified_results": [
    {
      "id": "ahmed.rashid@company.com",
      "name": "Ahmed Al-Rashid",
      "email_addresses": [
        {
          "address": "ahmed.rashid@company.com",
          "is_primary": true,
          "label": "Work"
        }
      ],
      "phone_numbers": [
        {
          "number": "+966-555-1234",
          "type": "business"
        }
      ],
      "organization": "Example Corp",
      "department": "Engineering",
      "job_title": "Senior Developer",
      "office_location": "Building A",
      "sources": ["gal", "email_history"],
      "is_vip": false,
      "communication_stats": {
        "total_emails": 45,
        "emails_sent": 20,
        "emails_received": 25,
        "first_contact": "2024-03-15T10:00:00",
        "last_contact": "2025-11-15T14:30:00",
        "emails_per_month": 3.75
      },
      "relationship_strength": 0.72
    }
  ]
}
```

### analyze_contacts

Unified contact analysis tool with multiple analysis types.

**v3.3 Changes:** Replaces `get_communication_history` and `analyze_network`.

**Input Schema:**
```json
{
  "analysis_type": "communication_history",  // communication_history, overview, top_contacts, by_domain, vip, dormant
  "email": "colleague@example.com",          // Required for communication_history
  "days_back": 90,
  "top_n": 20,
  "include_topics": true,
  "include_recent_emails": true,
  "vip_email_threshold": 10,
  "dormant_threshold_days": 60
}
```

**Response (communication_history):**
```json
{
  "success": true,
  "message": "Communication history retrieved",
  "email": "colleague@example.com",
  "stats": {
    "total_emails": 120,
    "emails_sent": 55,
    "emails_received": 65,
    "first_contact": "2024-01-10T09:00:00",
    "last_contact": "2025-11-17T16:45:00",
    "emails_per_month": 10.0
  },
  "timeline": [
    {"month": "2025-11", "count": 15},
    {"month": "2025-10", "count": 12}
  ],
  "top_topics": ["Project Update", "Meeting", "Review"]
}
```

**Response (overview):**
```json
{
  "success": true,
  "message": "Network analysis complete",
  "analysis_type": "overview",
  "summary": {
    "total_contacts": 245,
    "total_emails": 1250,
    "unique_domains": 35,
    "vip_contacts": 12,
    "dormant_contacts": 28
  },
  "top_contacts": [
    {
      "name": "John Doe",
      "email": "john@example.com",
      "email_count": 85
    }
  ],
  "top_domains": [
    {
      "domain": "example.com",
      "contact_count": 45,
      "email_count": 320
    }
  ]
}
```

## Email Tools

### send_email

Send an email through Exchange with optional attachments and CC/BCC. **Two-phase confirmed** (see [Two-phase confirmation](#two-phase-confirmation)) and subject to `SEND_ENABLED`, the capability tier, recipient allow/denylists, and `EWS_MAX_SENDS_PER_HOUR`.

**Input Schema:**
```json
{
  "to": ["recipient@example.com"],
  "subject": "Email Subject",
  "body": "Email body (HTML supported)",
  "cc": ["cc@example.com"],           // Optional
  "bcc": ["bcc@example.com"],         // Optional
  "importance": "Normal",             // Optional: Low, Normal, High
  "attachments": ["/path/to/file"],   // Optional
  "dry_run": false,                   // Optional: validate + preview, never send
  "confirm_token": "eyJ..."           // Phase-2 token; omit on the first call
}
```

The first call (no `confirm_token`) returns `{requires_confirmation: true, preview, confirm_token, expires_at, ttl_seconds}` and sends nothing; repeating it with the token and identical arguments sends.

**Response (phase 2):**
```json
{
  "success": true,
  "message": "Email sent successfully",
  "message_id": "AAMkAGI...",
  "sent_time": "2025-01-04T10:00:00",
  "recipients": ["recipient@example.com"]
}
```

### reply_email

Reply to an email, preserving the conversation thread (quoted headers, inline images, signature placement). **Two-phase confirmed** and send-gated like `send_email`.

**Input Schema:**
```json
{
  "message_id": "AAMkAGI...",        // Required: message to reply to (raw id or sid)
  "body": "<p>Reply text</p>",       // Required (HTML supported)
  "reply_all": false,                // true = reply to sender + all recipients
  "attachments": ["/path/to/file"],  // Optional
  "target_mailbox": "shared@example.com",
  "confirm_token": "eyJ..."          // Phase-2 token; omit on the first call
}
```

### forward_email

Forward an email to new recipients with the original content and attachments preserved. **Two-phase confirmed** and send-gated like `send_email`.

**Input Schema:**
```json
{
  "message_id": "AAMkAGI...",        // Required (raw id or sid)
  "to": ["recipient@example.com"],   // Required
  "body": "<p>FYI</p>",              // Optional note above the forwarded content
  "cc": ["cc@example.com"],          // Optional
  "bcc": ["bcc@example.com"],        // Optional
  "attachments": ["/path/to/file"],  // Optional additional attachments
  "target_mailbox": "shared@example.com",
  "confirm_token": "eyJ..."          // Phase-2 token; omit on the first call
}
```

### read_emails

Read emails from a specified folder, paginated.

**Input Schema:**
```json
{
  "folder": "inbox",          // Standard names (inbox, sent, drafts...), paths (Inbox/CC), or a folder ID
  "max_results": 50,          // Max: 1000
  "unread_only": false,
  "offset": 0,                // Pagination offset; pair with the response's next_offset
  "fields": ["subject", "from", "received_time"]   // Optional projection (default: light list fields, no body)
}
```

**Response:**
```json
{
  "success": true,
  "message": "Retrieved 10 emails",
  "items": [
    {
      "message_id": "AAMkAGI...",
      "sid": "m7",
      "subject": "Meeting Tomorrow",
      "from": "sender@example.com",
      "received_time": "2025-01-04T09:30:00",
      "is_read": false,
      "has_attachments": true,
      "snippet": "Please review the attached..."
    }
  ],
  "count": 10,
  "total_available": 245,
  "next_offset": 10,          // Present only when more items remain
  "emails": [ ... ],          // Legacy mirror of items (one-release deprecation window)
  "total_count": 10,          // Legacy mirror of count
  "folder": "inbox"
}
```

Failures are **classified, not silent**: a mid-page failure with nothing collected raises a tool error tagged `TIMEOUT`, `THROTTLED`, `CONNECTION`, `AUTH_EXPIRED`, or `UNKNOWN` — an empty folder still returns `success: true` with zero items, so "broken" and "empty" are distinguishable. A partial page carries `meta: {error_code, error_message}`. List snippets show the **latest reply only** (quoted history is not re-shipped).

### search_emails

Unified search with 3 modes: quick (default), advanced, and full_text.

**v3.3 Changes:** Replaces `advanced_search` and `full_text_search`. Use `mode` param to select search type.

**Input Schema (mode: "quick" — default):**
```json
{
  "mode": "quick",
  "folder": "inbox",
  "subject_contains": "report",
  "from_address": "boss@example.com",
  "has_attachments": true,
  "is_read": false,
  "start_date": "2025-01-01T00:00:00",
  "end_date": "2025-01-31T23:59:59",
  "max_results": 50
}
```

**Input Schema (mode: "advanced"):**
```json
{
  "mode": "advanced",
  "keywords": "quarterly report",
  "from_address": "boss@example.com",
  "folders": ["inbox", "sent"],
  "importance": "High",
  "categories": ["Work"],
  "sort_by": "datetime_received",
  "sort_order": "descending",
  "max_results": 100
}
```

**Input Schema (mode: "full_text"):**
```json
{
  "mode": "full_text",
  "search_query": "project budget",
  "folder": "inbox",
  "search_in": ["subject", "body"],
  "exact_phrase": false,
  "max_results": 50
}
```

### get_email_details

Get full details of a specific email.

**Input Schema:**
```json
{
  "message_id": "AAMkAGI...",                 // Required (raw id or sid)
  "fields": ["subject", "from", "body"],      // Optional projection of the email object
  "format": "full"                            // full (default) | clean
}
```

**Response (format: "full"):**
```json
{
  "success": true,
  "message": "Email details retrieved",
  "email": {
    "message_id": "AAMkAGI...",
    "sid": "m7",
    "subject": "Project Update",
    "from": "sender@example.com",
    "to": ["you@example.com"],
    "cc": ["team@example.com"],
    "body": "Full email body text",
    "body_html": "<html>...</html>",
    "received_time": "2025-01-04T09:00:00",
    "sent_time": "2025-01-04T08:58:00",
    "is_read": true,
    "has_attachments": true,
    "importance": "High",
    "attachments": ["report.pdf", "data.xlsx"],
    "internet_message_id": "<CAF1x...@mail.example.com>"
  }
}
```

`internet_message_id` (the RFC-5322 Message-ID) is always included — unlike EWS ItemIds it survives folder moves.

**`format: "clean"`** is the token-lean reading view: quoted reply history and signature stripped, `body_html` dropped, body truncated at a word boundary (~4000 chars). When quoting was stripped, the email object notes it:

```json
{
  "email": {
    "body": "Latest reply text only",
    "quoted_history": "stripped 3 quoted block(s) — use get_thread for the full conversation",
    "body_truncated": true,
    "internet_message_id": "<CAF1x...@mail.example.com>"
  }
}
```

### delete_email

Delete an email (soft delete to trash or permanent delete).

**Input Schema:**
```json
{
  "message_id": "AAMkAGI...",   // Raw id or sid
  "permanent": false,           // true = HARD delete (two-phase). Alias: hard_delete
  "hard_delete": false,         // Alias for permanent
  "confirm_token": "eyJ..."     // Phase-2 token for permanent delete
}
```

Soft delete (default) moves the message to Deleted Items, is reversible, and executes immediately. `permanent: true` is gated by [two-phase confirmation](#two-phase-confirmation): the first call returns a preview + `confirm_token` and deletes nothing; repeating with the token deletes.

### move_email

Move an email to a different folder.

**Input Schema:**
```json
{
  "message_id": "AAMkAGI...",
  "destination_folder": "sent"    // inbox, sent, drafts, deleted, junk
}
```

### update_email

Update email properties such as read status, flags, categories, and importance.

**Input Schema:**
```json
{
  "message_id": "AAMkAGI...",
  "is_read": true,                         // Optional: mark as read/unread
  "categories": ["Important", "Work"],     // Optional: email categories/labels
  "flag_status": "Flagged",                // Optional: NotFlagged, Flagged, Complete
  "importance": "High"                     // Optional: Low, Normal, High
}
```

**Response:**
```json
{
  "success": true,
  "message": "Email updated successfully",
  "message_id": "AAMkAGI...",
  "updates": {
    "is_read": true,
    "categories": ["Important", "Work"],
    "flag_status": "Flagged",
    "importance": "High"
  }
}
```

## Batch Email Tools

Plural counterparts of `update_email` / `move_email` / `delete_email` — act on up to **50 message ids** in one call. Each returns **per-item** results so a partial failure never sinks the batch, plus summary counts (`count`, `ok_count`, `error_count`). All accept raw ids or sids in `message_ids`.

### update_messages

Bulk mark read/unread, (re)categorize, flag, or set importance. Requires at least one of `is_read`, `categories`, `flag_status`, `importance`.

**Input Schema:**
```json
{
  "message_ids": ["AAMkAGF1...", "m12"],   // Required, max 50
  "is_read": true,
  "categories": ["Work"],
  "flag_status": "Flagged",                // NotFlagged, Flagged, Complete
  "importance": "High",                    // Low, Normal, High
  "target_mailbox": "shared@example.com"
}
```

**Response:**
```json
{
  "success": true,
  "message": "Bulk update complete",
  "results": [
    { "message_id": "AAMkAGF1...", "ok": true, "updates": { "is_read": true } },
    { "message_id": "m12", "ok": false, "error": "ErrorItemNotFound: ..." }
  ],
  "count": 2,
  "ok_count": 1,
  "error_count": 1
}
```

### move_messages

Bulk move messages to a folder (by name/path or id). EWS ItemIds change on move — per-item results include the **new id** (`new_id`); sid aliases are rebound automatically so the handles you hold keep working.

**Input Schema:**
```json
{
  "message_ids": ["AAMkAGF1..."],          // Required, max 50
  "destination_folder": "Archive/2026",    // Folder name or path...
  "destination_folder_id": "AAMkAGF...",   // ...or folder id (alternative)
  "target_mailbox": "shared@example.com"
}
```

**Response:**
```json
{
  "success": true,
  "message": "Bulk move to Archive/2026 complete",
  "results": [
    { "message_id": "AAMkAGF1...", "ok": true, "new_id": "AAMkNEW..." }
  ],
  "destination_folder": "Archive/2026",
  "count": 1,
  "ok_count": 1,
  "error_count": 0
}
```

### delete_messages

Bulk delete. Soft delete (default) moves to Deleted Items and is reversible. `permanent: true` (alias `hard_delete`) is a HARD delete gated by [two-phase confirmation](#two-phase-confirmation): the first call (no `confirm_token`) returns a preview + token bound to the exact id set; the second call with the token deletes.

**Input Schema:**
```json
{
  "message_ids": ["AAMkAGF1..."],   // Required, max 50
  "permanent": false,               // Hard delete (two-phase). Alias: hard_delete
  "confirm_token": "eyJ...",        // Phase-2 token for hard delete
  "target_mailbox": "shared@example.com"
}
```

**Response:**
```json
{
  "success": true,
  "message": "Bulk delete complete",
  "results": [
    { "message_id": "AAMkAGF1...", "ok": true, "action": "moved_to_trash" }
  ],
  "permanent": false,
  "count": 1,
  "ok_count": 1,
  "error_count": 0
}
```

(`action` is `"permanently_deleted"` for hard deletes.)

### list_attachments

List all attachments for a specific email message.

**Input Schema:**
```json
{
  "message_id": "AAMkAGI...",
  "include_inline": true        // Optional: include inline images
}
```

**Response:**
```json
{
  "success": true,
  "message": "Found 2 attachment(s)",
  "message_id": "AAMkAGI...",
  "count": 2,
  "attachments": [
    {
      "id": "AAMkAGI1AAAA=",
      "name": "report.pdf",
      "size": 245760,
      "content_type": "application/pdf",
      "is_inline": false,
      "content_id": null
    },
    {
      "id": "AAMkAGI2AAAA=",
      "name": "logo.png",
      "size": 8192,
      "content_type": "image/png",
      "is_inline": true,
      "content_id": "image001"
    }
  ]
}
```

### download_attachment

Download an email attachment as base64 or save to file.

**Input Schema:**
```json
{
  "message_id": "AAMkAGI...",
  "attachment_id": "AAMkAGI1AAAA=",
  "return_as": "base64",           // base64 or file_path
  "save_path": "/path/to/file"     // Required if return_as is file_path
}
```

**Response (base64):**
```json
{
  "success": true,
  "message": "Attachment downloaded successfully",
  "message_id": "AAMkAGI...",
  "attachment_id": "AAMkAGI1AAAA=",
  "name": "report.pdf",
  "size": 245760,
  "content_type": "application/pdf",
  "content_base64": "JVBERi0xLjQKJeLjz9MKMy..."
}
```

**Response (file_path):**
```json
{
  "success": true,
  "message": "Attachment saved successfully",
  "message_id": "AAMkAGI...",
  "attachment_id": "AAMkAGI1AAAA=",
  "name": "report.pdf",
  "size": 245760,
  "content_type": "application/pdf",
  "file_path": "/downloads/report.pdf"
}
```

## Calendar Tools

### create_appointment

Create a calendar appointment or meeting.

**Input Schema:**
```json
{
  "subject": "Team Standup",
  "start_time": "2025-01-05T10:00:00",
  "end_time": "2025-01-05T10:30:00",
  "location": "Conference Room A",
  "body": "Daily standup meeting",
  "attendees": ["team1@example.com", "team2@example.com"],
  "is_all_day": false,
  "reminder_minutes": 15
}
```

**Response:**
```json
{
  "success": true,
  "message": "Appointment created successfully",
  "item_id": "AAMkAGV...",
  "subject": "Team Standup",
  "start_time": "2025-01-05T10:00:00",
  "end_time": "2025-01-05T10:30:00"
}
```

### get_calendar

Retrieve calendar events for a date range.

**Input Schema:**
```json
{
  "start_date": "2025-01-01T00:00:00",    // Optional, defaults to today
  "end_date": "2025-01-07T23:59:59",      // Optional, defaults to +7 days
  "max_results": 50
}
```

**Response:**
```json
{
  "success": true,
  "message": "Retrieved 5 events",
  "events": [
    {
      "item_id": "AAMkAGV...",
      "subject": "Team Meeting",
      "start": "2025-01-05T10:00:00",
      "end": "2025-01-05T11:00:00",
      "location": "Room A",
      "organizer": "manager@example.com",
      "is_all_day": false,
      "attendees": ["you@example.com", "colleague@example.com"]
    }
  ],
  "start_date": "2025-01-01T00:00:00",
  "end_date": "2025-01-07T23:59:59"
}
```

### get_event

Get full detail for a single calendar event by id: attendees with response status, organizer, body, recurrence flags, and iCal uid. Complements `get_calendar` (which lists a range).

**Input Schema:**
```json
{
  "item_id": "AAMkAGV...",     // Calendar item id from get_calendar (raw id or sid)
  "event_id": "AAMkAGV...",    // Alias for item_id
  "max_body_chars": 4000,      // 0 omits the body
  "target_mailbox": "shared@example.com"
}
```

**Response:**
```json
{
  "success": true,
  "message": "Event retrieved",
  "event": {
    "item_id": "AAMkAGV...",
    "sid": "e3",
    "ical_uid": "040000008200E0...",
    "subject": "Team Meeting",
    "start": "2026-06-15T10:00:00+03:00",
    "end": "2026-06-15T11:00:00+03:00",
    "location": "Room A",
    "is_all_day": false,
    "is_recurring": false,
    "is_meeting": true,
    "organizer": "manager@example.com",
    "my_response": "Accept",
    "required_attendees": [
      { "email": "you@example.com", "name": "You", "response": "Accept" }
    ],
    "optional_attendees": [],
    "body": "Agenda...",
    "body_truncated": false
  }
}
```

### update_appointment

Update an existing appointment.

**Input Schema:**
```json
{
  "item_id": "AAMkAGV...",
  "subject": "Updated Meeting Title",
  "start_time": "2025-01-05T11:00:00",
  "end_time": "2025-01-05T12:00:00",
  "location": "Room B",
  "body": "Updated description"
}
```

### delete_appointment

Delete a calendar appointment.

**Input Schema:**
```json
{
  "item_id": "AAMkAGV...",
  "send_cancellation": true    // Send cancellation to attendees
}
```

### respond_to_meeting

Respond to a meeting invitation. Accept/decline transmits a response message to the organizer immediately, so this is send-class and **two-phase confirmed** (see [Two-phase confirmation](#two-phase-confirmation)): the first call previews; repeat with `confirm_token` to send the response.

**Input Schema:**
```json
{
  "item_id": "AAMkAGV...",     // Raw id or sid
  "response": "Accept",        // Accept, Tentative, Decline
  "message": "I'll be there",  // Optional response message
  "confirm_token": "eyJ..."    // Phase-2 token; omit on the first call
}
```

### check_availability

Get free/busy information for one or more users in a specified time range.

**Input Schema:**
```json
{
  "email_addresses": ["user1@example.com", "user2@example.com"],
  "start_time": "2025-01-15T09:00:00+00:00",
  "end_time": "2025-01-15T17:00:00+00:00",
  "interval_minutes": 30        // Optional: time slot granularity (15-1440)
}
```

**Response:**
```json
{
  "success": true,
  "message": "Availability retrieved for 2 user(s)",
  "availability": [
    {
      "email": "user1@example.com",
      "view_type": "Detailed",
      "merged_free_busy": "00002222000011110000",
      "free_busy_legend": {
        "0": "Free",
        "1": "Tentative",
        "2": "Busy",
        "3": "OutOfOffice",
        "4": "NoData"
      },
      "calendar_events": [
        {
          "start": "2025-01-15T10:00:00+00:00",
          "end": "2025-01-15T11:00:00+00:00",
          "busy_type": "Busy",
          "details": null
        }
      ]
    }
  ],
  "time_range": {
    "start": "2025-01-15T09:00:00+00:00",
    "end": "2025-01-15T17:00:00+00:00",
    "interval_minutes": 30
  }
}
```

## Contact Tools

### create_contact

Create a new contact in Exchange.

**Input Schema:**
```json
{
  "given_name": "John",
  "surname": "Doe",
  "email_address": "john.doe@example.com",
  "phone_number": "+1-555-0100",
  "company": "Acme Corp",
  "job_title": "Software Engineer",
  "department": "Engineering"
}
```

### update_contact

Update an existing contact.

**Input Schema:**
```json
{
  "item_id": "AAMkAGU...",
  "given_name": "Jane",
  "job_title": "Senior Engineer",
  "phone_number": "+1-555-0101"
}
```

### delete_contact

Delete a contact.

**Input Schema:**
```json
{
  "item_id": "AAMkAGU..."
}
```

> **Note:** `search_contacts`, `get_contacts`, and `resolve_names` have been merged into `find_person` (see Contact Intelligence Tools above). Use `find_person(source="contacts")` to list contacts, `find_person(source="gal")` for GAL resolution.

## Task Tools

### create_task

Create a new task.

**Input Schema:**
```json
{
  "subject": "Complete project report",
  "body": "Q4 financial report",
  "due_date": "2025-01-31T17:00:00",
  "start_date": "2025-01-15T09:00:00",
  "importance": "High",
  "reminder_time": "2025-01-30T09:00:00"
}
```

### get_tasks

List tasks with optional filtering.

**Input Schema:**
```json
{
  "include_completed": false,
  "max_results": 50
}
```

**Response:**
```json
{
  "success": true,
  "message": "Retrieved 3 tasks",
  "tasks": [
    {
      "item_id": "AAMkAGT...",
      "subject": "Complete report",
      "status": "InProgress",
      "percent_complete": 50,
      "is_complete": false,
      "due_date": "2025-01-31T17:00:00",
      "importance": "High"
    }
  ]
}
```

### update_task

Update an existing task.

**Input Schema:**
```json
{
  "item_id": "AAMkAGT...",
  "subject": "Updated task name",
  "percent_complete": 75,
  "importance": "Normal"
}
```

### complete_task

Mark a task as complete.

**Input Schema:**
```json
{
  "item_id": "AAMkAGT..."
}
```

### delete_task

Delete a task.

**Input Schema:**
```json
{
  "item_id": "AAMkAGT..."
}
```

## Search Tools

> **Note:** `advanced_search` and `full_text_search` have been merged into `search_emails` (see Email Tools above). Use `search_emails(mode="advanced")` for multi-criteria search and `search_emails(mode="full_text")` for full-text search.

## Folder Tools

### list_folders

Get mailbox folder hierarchy with configurable depth and details.

**Input Schema:**
```json
{
  "parent_folder": "root",         // root, inbox, sent, drafts, deleted, junk, calendar, contacts, tasks
  "depth": 2,                      // 1-10: folder tree traversal depth
  "include_hidden": false,         // Include system/hidden folders
  "include_counts": true           // Include item and unread counts
}
```

**Response:**
```json
{
  "success": true,
  "message": "Listed 12 folder(s)",
  "total_folders": 12,
  "parent_folder": "root",
  "depth": 2,
  "folder_tree": {
    "id": "AAMkAGF...",
    "name": "Root",
    "parent_folder_id": "",
    "folder_class": "IPF",
    "child_folder_count": 4,
    "total_count": 0,
    "unread_count": 0,
    "children": [
      {
        "id": "AAMkAGI...",
        "name": "Inbox",
        "parent_folder_id": "AAMkAGF...",
        "folder_class": "IPF.Note",
        "child_folder_count": 3,
        "total_count": 245,
        "unread_count": 12,
        "children": [
          {
            "id": "AAMkAGJ...",
            "name": "Projects",
            "parent_folder_id": "AAMkAGI...",
            "folder_class": "IPF.Note",
            "child_folder_count": 0,
            "total_count": 48,
            "unread_count": 5
          }
        ]
      },
      {
        "id": "AAMkAGK...",
        "name": "Sent Items",
        "parent_folder_id": "AAMkAGF...",
        "folder_class": "IPF.Note",
        "child_folder_count": 0,
        "total_count": 532,
        "unread_count": 0
      }
    ]
  }
}
```

### manage_folder

Unified folder management with 4 actions: create, delete, rename, move.

**v3.3 Changes:** Replaces `create_folder`, `delete_folder`, `rename_folder`, `move_folder`.

**v3.5 Changes:** `action: "delete"` (the one irreversible action — it deletes the folder *and its contents*) is gated by [two-phase confirmation](#two-phase-confirmation): the first call returns a preview + `confirm_token`; repeat with the token to delete. Other actions execute immediately.

**Input Schema (action: "create"):**
```json
{
  "action": "create",
  "folder_name": "Projects",
  "parent_folder": "inbox",
  "folder_class": "IPF.Note"
}
```

**Input Schema (action: "delete"):**
```json
{
  "action": "delete",
  "folder_id": "AAMkAGF...",
  "permanent": false
}
```

**Input Schema (action: "rename"):**
```json
{
  "action": "rename",
  "folder_id": "AAMkAGF...",
  "new_name": "Archived Projects"
}
```

**Input Schema (action: "move"):**
```json
{
  "action": "move",
  "folder_id": "AAMkAGF...",
  "destination": "archive"
}
```

### find_folder

Locate a folder by name or ID anywhere in the mailbox hierarchy. Returns the folder ID, full path, parent, and (when available) item count.

**Input Schema:**
```json
{
  "folder_name": "Archive/Q1 Reports",   // Optional: name or path
  "folder_id": "AAMkAGF...",             // Optional: resolve by stable ID
  "target_mailbox": "other@example.com"  // Optional: impersonation
}
```

**Response:**
```json
{
  "success": true,
  "folder": {
    "folder_id": "AAMkAGF...",
    "name": "Q1 Reports",
    "path": "Archive/Q1 Reports",
    "parent_id": "AAMkAGF...",
    "item_count": 42,
    "child_count": 0
  }
}
```

Use `folder_id` from this response with `move_email`, `copy_email` (`destination_folder_id`), or `manage_folder` (`parent_folder_id`) for robust folder references.

## Email Drafts

Draft tools let an AI assistant prepare a message in the Drafts folder for the user to review/edit/send. Nothing leaves the mailbox until it is explicitly sent.

### create_draft

Create a new email draft.

**Input Schema:**
```json
{
  "to": ["recipient@example.com"],          // Required
  "subject": "Draft subject",                // Required (min length 1)
  "body": "<p>Draft body (HTML supported)</p>", // Required
  "cc": ["cc@example.com"],
  "bcc": ["bcc@example.com"],
  "importance": "Normal",                    // Low / Normal / High
  "attachments": ["/path/to/file.pdf"],
  "inline_attachments": [
    { "file_name": "logo.png", "content_id": "logo1", "file_content": "base64..." }
  ],
  "target_mailbox": "shared@example.com"
}
```

**Response:**
```json
{ "success": true, "draft_id": "AAMkAGF...", "folder": "drafts" }
```

### create_reply_draft

Build a reply draft for AI preview-before-send. Preserves original conversation threading, quoted headers (From/To/Sent/Subject), inline images, and signature placement.

**Input Schema:**
```json
{
  "message_id": "AAMkAGF...",       // Required: original message
  "body": "<p>Your draft reply</p>", // Required
  "to_all": false,                   // Reply-all vs. reply to sender
  "subject": "Optional override",
  "attachments": ["/path/to/file.pdf"],
  "target_mailbox": "shared@example.com"
}
```

**Response:** `{ "success": true, "draft_id": "AAMkAGF..." }`

### create_forward_draft

Build a forward draft (full body + inline images + attachments preserved).

**Input Schema:**
```json
{
  "message_id": "AAMkAGF...",      // Required: original message
  "to": ["recipient@example.com"],  // Required: forward recipients
  "body": "<p>Forward message</p>", // Required
  "subject": "Optional override",
  "attachments": ["/path/to/file.pdf"],
  "target_mailbox": "shared@example.com"
}
```

**Response:** `{ "success": true, "draft_id": "AAMkAGF..." }`

### update_draft

Edit an existing draft's subject / to / cc / bcc / body / importance by id. Reversible; does not send. Each provided field replaces the draft's; omitted fields are left unchanged.

**Input Schema:**
```json
{
  "draft_id": "AAMkAGF...",            // Required: message_id of the draft (raw id or sid)
  "subject": "New subject",
  "to": ["a@example.com"],
  "cc": ["cc@example.com"],
  "bcc": ["bcc@example.com"],
  "body": "<p>Replacement body</p>",   // HTML auto-detected
  "importance": "High",                // Low / Normal / High
  "target_mailbox": "shared@example.com"
}
```

**Response:**
```json
{
  "success": true,
  "message": "Draft updated.",
  "draft_id": "AAMkAGF...",
  "updated": { "subject": "New subject", "body_chars": 120 }
}
```

### send_draft

Send an existing draft (from `create_draft` / `create_reply_draft` / `create_forward_draft`) by its id. **Two-phase with a content-bound token**: phase 1 (no `confirm_token`) returns a preview + short-lived token and sends nothing; phase 2 (with the token) sends. The token is bound to the draft's current subject/recipients/body, so editing the draft (e.g. via `update_draft`) invalidates it and forces a fresh preview. Blocked entirely when `SEND_ENABLED=false`.

**Input Schema:**
```json
{
  "draft_id": "AAMkAGF...",       // Required (raw id or sid)
  "confirm_token": "eyJ...",      // Omit on the first call
  "idempotency_key": "uuid-...",  // Optional: client UUID for safe retries
  "target_mailbox": "shared@example.com"
}
```

**Response (phase 1):**
```json
{
  "success": true,
  "requires_confirmation": true,
  "confirm_token": "eyJ...{signature}",
  "expires_at": 1781000000,
  "ttl_seconds": 600,
  "preview": {
    "draft_id": "AAMkAGF...",
    "to": ["a@example.com"],
    "cc": [],
    "bcc": [],
    "subject": "Quarterly report",
    "body_preview": "First 1000 chars...",
    "attachment_count": 1
  }
}
```

**Response (phase 2):**
```json
{
  "success": true,
  "message": "Draft sent.",
  "sent": true,
  "draft_id": "AAMkAGF...",
  "message_id": "<CAF1x...@mail.example.com>",
  "to": ["a@example.com"],
  "cc": [],
  "subject": "Quarterly report"
}
```

With `idempotency_key`, a retry carrying the same key against an unchanged draft returns the cached receipt with `"idempotent_replay": true` instead of sending again.

## Enhanced Attachment Tools

### add_attachment

Add attachments to draft or existing emails via file path or base64 content.

**Input Schema:**
```json
{
  "message_id": "AAMkAGF...",          // Required: Email message ID
  "file_path": "/path/to/file.pdf",    // Provide either file_path...
  "file_content": "base64string...",   // ...or base64 encoded content
  "file_name": "document.pdf",         // Required if using file_content
  "content_type": "application/pdf",   // Optional: MIME type (auto-detected for file_path)
  "is_inline": false,                  // Optional: Inline attachment (default: false)
  "content_id": "image123"             // Optional: Content ID for inline images
}
```

**Response:**
```json
{
  "success": true,
  "message": "Attachment added successfully",
  "attachment_id": "AAMkAGA...",
  "attachment_name": "document.pdf",
  "message_id": "AAMkAGF...",
  "size_bytes": 245760,
  "is_inline": false
}
```

### delete_attachment

Remove attachments from emails by attachment ID or name.

**Input Schema:**
```json
{
  "message_id": "AAMkAGF...",          // Required: Email message ID
  "attachment_id": "AAMkAGA...",       // Provide either attachment_id...
  "attachment_name": "old_file.pdf"    // ...or attachment name
}
```

**Response:**
```json
{
  "success": true,
  "message": "Attachment deleted successfully",
  "attachment_id": "AAMkAGA...",
  "attachment_name": "old_file.pdf",
  "message_id": "AAMkAGF..."
}
```

### get_email_mime

Return the full RFC-822 MIME content of a message (useful for archival, forensics, and external processors that expect `.eml` input).

**Input Schema:**
```json
{
  "message_id": "AAMkAGF...",
  "target_mailbox": "shared@example.com"
}
```

**Response:**
```json
{
  "success": true,
  "message_id": "AAMkAGF...",
  "mime_content": "Received: from ...\r\nFrom: ...\r\nTo: ...\r\n\r\n<body>",
  "size_bytes": 12345
}
```

### attach_email_to_draft

Attach an existing message (as `.eml`) to a draft. Use in combination with `create_draft` / `create_reply_draft` / `create_forward_draft` when you want to forward/quote a full message as an attachment rather than inline.

**Input Schema:**
```json
{
  "draft_id": "AAMkAGF-draft-id...",
  "message_id_to_attach": "AAMkAGF-original-msg...",
  "target_mailbox": "shared@example.com"
}
```

**Response:**
```json
{
  "success": true,
  "draft_id": "AAMkAGF-draft-id...",
  "attachment_name": "Fwd_original_subject.eml"
}
```

## Advanced Search Tools

### search_by_conversation

Find all emails in a conversation thread.

**Input Schema:**
```json
{
  "conversation_id": "AAQkAGF...",     // Provide either conversation_id...
  "message_id": "AAMkAGF...",          // ...or message_id (will extract conversation_id)
  "folder": "inbox",                   // Optional: Folder to search (default: inbox)
  "max_results": 100                   // Optional: Maximum results (default: 100)
}
```

**Response:**
```json
{
  "success": true,
  "message": "Found 5 emails in conversation",
  "conversation_id": "AAQkAGF...",
  "thread_count": 5,
  "emails": [
    {
      "id": "AAMkAGF1...",
      "subject": "Project Discussion",
      "from": "alice@example.com",
      "to": ["bob@example.com"],
      "datetime_received": "2025-01-10T09:00:00+00:00",
      "preview": "Let's discuss the project timeline...",
      "is_read": true,
      "has_attachments": false
    },
    {
      "id": "AAMkAGF2...",
      "subject": "RE: Project Discussion",
      "from": "bob@example.com",
      "to": ["alice@example.com"],
      "datetime_received": "2025-01-10T10:15:00+00:00",
      "preview": "Sounds good. I propose we...",
      "is_read": true,
      "has_attachments": true
    }
  ]
}
```

### get_thread

Reconstruct a whole conversation into one thread object: participants, date range, and the messages **oldest→newest**. Pass `message_id` OR `conversation_id` (raw ids or sids). The single highest-value read for a secretary-style agent.

**Input Schema:**
```json
{
  "message_id": "AAMkAGF...",       // Any message in the thread...
  "conversation_id": "AAQkAGF...",  // ...or the conversation id (alternative)
  "max_messages": 50,               // 1-200
  "bodies": "latest",               // none | latest (default: full text only for the newest message) | all
  "clean_bodies": true,             // Default true: strip quoted history + signature from each body
  "target_mailbox": "shared@example.com"
}
```

**Response:**
```json
{
  "success": true,
  "message": "Thread with 5 message(s)",
  "thread_id": "AAQkAGF...",
  "thread_sid": "t1",
  "subject": "RE: Project Discussion",
  "message_count": 5,
  "participants": ["alice@example.com", "bob@example.com"],
  "date_range": ["2026-01-10T09:00:00+00:00", "2026-01-12T16:45:00+00:00"],
  "messages": [
    { "message_id": "AAMkAGF1...", "sid": "m3", "subject": "Project Discussion", "from": "alice@example.com", "received_time": "2026-01-10T09:00:00+00:00", "snippet": "..." }
  ],
  "latest": { "message_id": "AAMkAGF5...", "sid": "m9", "body": "Latest reply text", "quoted_stripped": 4 },
  "truncated": false
}
```

With `clean_bodies: true` (default) each returned body has its quoted reply history and signature stripped — the older messages are already in the thread, so re-shipping the quoted chain is pure redundancy. Stripped messages carry `quoted_stripped` (count of removed blocks) and `body_truncated` when capped. Pass `clean_bodies: false` for raw text bodies.

> **Note:** `full_text_search` has been merged into `search_emails(mode="full_text")`. See Email Tools above.

## Out-of-Office Tools

### oof_settings

Unified Out-of-Office tool with get and set actions.

**v3.3 Changes:** Replaces `set_oof_settings` and `get_oof_settings`.

**v3.5 Changes:** `action: "set"` changes externally visible auto-reply behavior for every correspondent, so it is gated by [two-phase confirmation](#two-phase-confirmation): the first call returns a preview + `confirm_token`; repeat with the token to apply. `action: "get"` is ungated.

**Input Schema (action: "set"):**
```json
{
  "action": "set",
  "state": "Scheduled",
  "internal_reply": "I'm out of the office",
  "external_reply": "I'm currently away",
  "start_time": "2025-12-20T00:00:00+00:00",
  "end_time": "2025-12-31T23:59:59+00:00",
  "external_audience": "Known"
}
```

**Input Schema (action: "get"):**
```json
{
  "action": "get"
}
```

**Response:**
```json
{
  "success": true,
  "message": "Current OOF state: Scheduled",
  "settings": {
    "state": "Scheduled",
    "internal_reply": "I'm out of the office",
    "external_reply": "I'm currently away",
    "external_audience": "Known",
    "start_time": "2025-12-20T00:00:00+00:00",
    "end_time": "2025-12-31T23:59:59+00:00",
    "currently_active": false
  }
}
```

## Calendar Enhancement Tools

### find_meeting_times

AI-powered meeting time finder that analyzes attendee availability and suggests optimal meeting slots with intelligent scoring.

**Input Schema:**
```json
{
  "attendees": [                        // Required: List of attendee emails (1-20)
    "alice@example.com",
    "bob@example.com",
    "carol@example.com"
  ],
  "duration_minutes": 60,               // Required: Meeting duration (15-480 minutes)
  "max_suggestions": 5,                 // Optional: Number of suggestions (default: 5)
  "start_date": "2025-01-15T00:00:00", // Optional: Search start (default: tomorrow)
  "end_date": "2025-01-20T23:59:59",   // Optional: Search end (default: 7 days from start)
  "preferences": {                      // Optional: Scheduling preferences
    "prefer_morning": true,             // Prefer morning slots (before 12 PM)
    "prefer_afternoon": false,          // Prefer afternoon slots (after 1 PM)
    "working_hours_start": 9,           // Working hours start (default: 8)
    "working_hours_end": 17,            // Working hours end (default: 18)
    "avoid_lunch": true,                // Avoid 12-1 PM time slots
    "min_gap_minutes": 15               // Minimum gap between meetings
  }
}
```

**Response:**
```json
{
  "success": true,
  "message": "Found 5 meeting time suggestions",
  "duration_minutes": 60,
  "attendee_count": 3,
  "suggestions": [
    {
      "start_time": "2025-01-15T10:00:00+00:00",
      "end_time": "2025-01-15T11:00:00+00:00",
      "duration_minutes": 60,
      "score": 95,
      "day_of_week": "Wednesday",
      "time_of_day": "Morning",
      "all_attendees_free": true
    },
    {
      "start_time": "2025-01-15T14:00:00+00:00",
      "end_time": "2025-01-15T15:00:00+00:00",
      "duration_minutes": 60,
      "score": 85,
      "day_of_week": "Wednesday",
      "time_of_day": "Afternoon",
      "all_attendees_free": true
    }
  ],
  "suggestion_count": 5,
  "search_period": {
    "start": "2025-01-15T00:00:00+00:00",
    "end": "2025-01-20T23:59:59+00:00"
  }
}
```

## Email Enhancement Tools

### copy_email

Copy an email to another folder while preserving the original.

**Input Schema:**
```json
{
  "message_id": "AAMkAGF...",          // Required: Email message ID
  "destination_folder": "archive"      // Required: Destination folder name
}
```

**Response:**
```json
{
  "success": true,
  "message": "Email copied successfully",
  "message_id": "AAMkAGF...",
  "copied_message_id": "AAMkAGH...",
  "source_folder": "inbox",
  "destination_folder": "archive",
  "subject": "Important Document"
}
```

## AI Tools (optional)

Disabled by default. Enable via `ENABLE_AI=true` plus the per-feature flag listed below. Requires `AI_PROVIDER`, `AI_API_KEY`, and `AI_MODEL` (and `AI_EMBEDDING_MODEL` for semantic search).

> AI tools do **not** accept `target_mailbox`; they operate on the primary authenticated mailbox.

### semantic_search_emails

Search emails by semantic similarity using embeddings. Enabled by `ENABLE_SEMANTIC_SEARCH=true`.

**Input Schema:**
```json
{
  "query": "invoices about Q1 cloud spend",  // Required: natural-language query
  "folder": "inbox",                          // Optional: inbox (default) / sent / drafts / all
  "max_results": 10,                          // 1-100
  "threshold": 0.7                            // 0.0-1.0 similarity cutoff
}
```

**Response:**
```json
{
  "success": true,
  "query": "invoices about Q1 cloud spend",
  "total_results": 3,
  "results": [
    {
      "message_id": "AAMkAGF...",
      "subject": "AWS Q1 invoice",
      "from": "billing@aws.amazon.com",
      "similarity_score": 0.89,
      "received_time": "2026-03-05T10:00:00"
    }
  ]
}
```

### classify_email

Classify an email's priority, sentiment, and (optionally) spam probability. Enabled by `ENABLE_EMAIL_CLASSIFICATION=true`.

**Input Schema:**
```json
{
  "message_id": "AAMkAGF...",
  "include_spam_detection": true
}
```

**Response:**
```json
{
  "success": true,
  "classification": {
    "priority": "high",
    "sentiment": "neutral",
    "category": "customer_support",
    "spam_probability": 0.03
  }
}
```

### summarize_email

Generate an AI summary of an email. Enabled by `ENABLE_EMAIL_SUMMARIZATION=true`.

**Input Schema:**
```json
{
  "message_id": "AAMkAGF...",
  "max_length": 200           // 50-500 characters
}
```

**Response:**
```json
{
  "success": true,
  "summary": "Customer reports login failure after yesterday's deploy; wants ETA for fix."
}
```

### suggest_replies

Generate N draft reply variants. Enabled by `ENABLE_SMART_REPLIES=true`.

**Input Schema:**
```json
{
  "message_id": "AAMkAGF...",
  "num_suggestions": 3        // 1-5
}
```

**Response:**
```json
{
  "success": true,
  "suggestions": [
    "Thanks for the update — will review and get back by EOD.",
    "Acknowledged. Can we sync tomorrow at 10 to walk through the details?",
    "Received; looping in the platform team for a second opinion."
  ]
}
```

## Error Responses

All tools return error responses in the following format:

```json
{
  "success": false,
  "error": "Human-readable message (max 200 chars)"
}
```

### Common Error Types

- `ValidationError`: Invalid input parameters
- `AuthenticationError`: Authentication failed
- `ConnectionError`: Cannot connect to Exchange
- `RateLimitError`: Too many requests
- `ToolExecutionError`: Tool execution failed

## Rate Limiting

Default: 25 requests per minute per user

When rate limited, you'll receive:
```json
{
  "success": false,
  "error": "Rate limit exceeded: maximum 25 requests per minute",
  "error_type": "RateLimitError",
  "is_retryable": true
}
```
