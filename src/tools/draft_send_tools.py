"""Send-an-existing-draft and edit-a-draft tools.

Completes the draft-first lifecycle:
    create_draft / create_reply_draft / create_forward_draft  →  (review)  →
    update_draft (optional edits)  →  send_draft (two-phase, gated, idempotent)

``send_draft`` is the Fable-design centerpiece: there is no one-shot send for
an existing draft — the model previews, a human/policy layer approves, and
only then does the second call transmit. It inherits the SEND_ENABLED kill
switch via ``side_effect_class = "send"``.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List

from exchangelib import Mailbox, HTMLBody, Body

from .base import BaseTool
from ..exceptions import ToolExecutionError
from ..utils import (
    format_success_response,
    safe_get,
    find_message_for_account,
    ews_id_to_str,
    truncate_text,
)
from ..safety import make_token, verify_token, content_hash


def _recipients(msg, attr: str) -> List[str]:
    return [
        r.email_address
        for r in (safe_get(msg, attr, []) or [])
        if r and getattr(r, "email_address", None)
    ]


def _draft_content_hash(subject, to, cc, bcc, body_text) -> str:
    # Recipients sorted so reordering doesn't invalidate a token, but adding
    # or removing a recipient does (that's a content change worth re-previewing).
    return content_hash(subject or "", sorted(to), sorted(cc), sorted(bcc), body_text or "")


class SendDraftTool(BaseTool):
    """Send an existing draft by id — two-phase, idempotent, kill-switch-gated."""

    side_effect_class = "send"

    def get_schema(self) -> Dict[str, Any]:
        return {
            "name": "send_draft",
            "description": (
                "Send an existing draft (from create_draft / create_reply_draft "
                "/ create_forward_draft) by its message_id. Two-phase: call once "
                "WITHOUT confirm_token to get a preview + short-lived token, then "
                "call again WITH the token to actually send. The token is bound "
                "to the draft's content, so editing the draft invalidates it. "
                "Blocked entirely when SEND_ENABLED=false. Pass idempotency_key "
                "to make retries safe against double-sending."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "draft_id": {
                        "type": "string",
                        "description": "message_id of the draft to send",
                    },
                    "confirm_token": {
                        "type": "string",
                        "description": "Token returned by the phase-1 preview. Omit on the first call.",
                    },
                    "idempotency_key": {
                        "type": "string",
                        "description": "Client UUID; a repeat with the same key + unchanged draft returns the cached receipt instead of sending again.",
                    },
                    "target_mailbox": {
                        "type": "string",
                        "description": "Send from a delegated/impersonated mailbox (requires impersonation).",
                    },
                },
                "required": ["draft_id"],
            },
        }

    async def execute(self, **kwargs) -> Dict[str, Any]:
        draft_id = kwargs.get("draft_id")
        confirm_token = kwargs.get("confirm_token")
        idempotency_key = kwargs.get("idempotency_key")
        target_mailbox = kwargs.get("target_mailbox")
        if not draft_id:
            raise ToolExecutionError("draft_id is required")

        account = self.get_account(target_mailbox)
        mailbox = self.get_mailbox_info(target_mailbox)

        try:
            draft = find_message_for_account(account, draft_id)
        except ToolExecutionError:
            raise ToolExecutionError(f"Draft not found: {draft_id}")

        subject = safe_get(draft, "subject", "") or ""
        to = _recipients(draft, "to_recipients")
        cc = _recipients(draft, "cc_recipients")
        bcc = _recipients(draft, "bcc_recipients")
        body_text = safe_get(draft, "text_body", None) or str(safe_get(draft, "body", "") or "")
        chash = _draft_content_hash(subject, to, cc, bcc, body_text)

        # Idempotency: replay a cached receipt for the same key + content.
        store = None
        if idempotency_key:
            try:
                store = self.get_memory_store()
                rec = store.get("idempotency", f"send.{idempotency_key}")
                if rec and isinstance(rec.value, dict) and rec.value.get("chash") == chash:
                    cached = dict(rec.value.get("result") or {})
                    cached["idempotent_replay"] = True
                    return cached
            except Exception as e:  # never block a send on a cache hiccup
                self.logger.warning(f"idempotency lookup failed (continuing): {e}")
                store = None

        # Phase 1: no token → preview + mint token, send nothing.
        if not confirm_token:
            tok = make_token(
                mailbox=mailbox, action="send_draft", target_id=str(draft_id), chash=chash
            )
            return format_success_response(
                "Confirmation required — review the preview, then call send_draft again with confirm_token.",
                requires_confirmation=True,
                confirm_token=tok["confirm_token"],
                expires_at=tok["expires_at"],
                ttl_seconds=tok["ttl_seconds"],
                preview={
                    "draft_id": str(draft_id),
                    "to": to,
                    "cc": cc,
                    "bcc": bcc,
                    "subject": subject,
                    "body_preview": truncate_text(body_text, 1000),
                    "attachment_count": len(safe_get(draft, "attachments", []) or []),
                },
                mailbox=mailbox,
            )

        # Phase 2: verify the token is bound to this draft's CURRENT content.
        ok, reason = verify_token(
            confirm_token,
            mailbox=mailbox,
            action="send_draft",
            target_id=str(draft_id),
            chash=chash,
        )
        if not ok:
            hint = {
                "expired": "Token expired — call send_draft without a token to get a fresh preview.",
                "stale": "Draft changed since the preview — re-preview to get a new token.",
                "bad_signature": "Invalid confirmation token.",
                "malformed": "Malformed confirmation token.",
            }.get(reason, "Invalid confirmation token.")
            raise ToolExecutionError(f"Confirmation failed ({reason}): {hint}")

        try:
            draft.send()
        except Exception as e:
            raise ToolExecutionError(f"Failed to send draft: {e}")

        result = format_success_response(
            "Draft sent.",
            sent=True,
            draft_id=str(draft_id),
            message_id=ews_id_to_str(safe_get(draft, "message_id", None)) or None,
            to=to,
            cc=cc,
            subject=subject,
            mailbox=mailbox,
        )

        if idempotency_key and store is not None:
            try:
                store.set(
                    "idempotency",
                    f"send.{idempotency_key}",
                    {"chash": chash, "result": result},
                    ttl_seconds=48 * 3600,
                )
            except Exception as e:  # the send already succeeded; don't fail it
                self.logger.warning(f"idempotency store failed (sent OK): {e}")

        return result


class UpdateDraftTool(BaseTool):
    """Edit an existing draft's recipients / subject / body / importance by id."""

    def get_schema(self) -> Dict[str, Any]:
        return {
            "name": "update_draft",
            "description": (
                "Edit an existing draft (subject, to/cc/bcc, body, importance) "
                "by its message_id. Reversible and does not send. Replaces each "
                "provided field; omitted fields are left unchanged."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "draft_id": {"type": "string", "description": "message_id of the draft to edit"},
                    "subject": {"type": "string"},
                    "to": {"type": "array", "items": {"type": "string"}},
                    "cc": {"type": "array", "items": {"type": "string"}},
                    "bcc": {"type": "array", "items": {"type": "string"}},
                    "body": {"type": "string", "description": "Replacement body (HTML auto-detected)."},
                    "importance": {"type": "string", "enum": ["Low", "Normal", "High"]},
                    "target_mailbox": {"type": "string"},
                },
                "required": ["draft_id"],
            },
        }

    async def execute(self, **kwargs) -> Dict[str, Any]:
        draft_id = kwargs.get("draft_id")
        if not draft_id:
            raise ToolExecutionError("draft_id is required")
        target_mailbox = kwargs.get("target_mailbox")
        account = self.get_account(target_mailbox)
        mailbox = self.get_mailbox_info(target_mailbox)

        try:
            draft = find_message_for_account(account, draft_id)
        except ToolExecutionError:
            raise ToolExecutionError(f"Draft not found: {draft_id}")

        update_fields: List[str] = []
        changed: Dict[str, Any] = {}

        if kwargs.get("subject") is not None:
            draft.subject = kwargs["subject"]
            update_fields.append("subject")
            changed["subject"] = kwargs["subject"]
        if kwargs.get("to") is not None:
            draft.to_recipients = [Mailbox(email_address=e) for e in kwargs["to"]]
            update_fields.append("to_recipients")
            changed["to"] = kwargs["to"]
        if kwargs.get("cc") is not None:
            draft.cc_recipients = [Mailbox(email_address=e) for e in kwargs["cc"]]
            update_fields.append("cc_recipients")
            changed["cc"] = kwargs["cc"]
        if kwargs.get("bcc") is not None:
            draft.bcc_recipients = [Mailbox(email_address=e) for e in kwargs["bcc"]]
            update_fields.append("bcc_recipients")
            changed["bcc"] = kwargs["bcc"]
        if kwargs.get("body") is not None:
            body = kwargs["body"].strip()
            is_html = bool(re.search(r"<[^>]+>", body))
            draft.body = HTMLBody(body) if is_html else Body(body)
            update_fields.append("body")
            changed["body_chars"] = len(body)
        if kwargs.get("importance") is not None:
            draft.importance = kwargs["importance"]
            update_fields.append("importance")
            changed["importance"] = kwargs["importance"]

        if not update_fields:
            raise ToolExecutionError(
                "No fields to update. Provide at least one of: subject, to, cc, "
                "bcc, body, importance."
            )

        try:
            draft.save(update_fields=update_fields)
        except Exception as e:
            raise ToolExecutionError(f"Failed to update draft: {e}")

        return format_success_response(
            "Draft updated.",
            draft_id=ews_id_to_str(safe_get(draft, "id", None)) or str(draft_id),
            updated=changed,
            mailbox=mailbox,
        )
