"""Batch mail-mutation tools — operate on many message ids in one call.

The single-item ``update_email`` / ``move_email`` / ``delete_email`` tools are
kept intact; these plural tools let a triage pass act on a whole page of
messages without one round-trip per message. Each returns **per-item**
results so a partial failure never sinks the batch, and ``move_messages``
returns the **id remap** explicitly (EWS ItemIds change on move).

Hard delete is two-phase (confirm token), matching the safety ladder; soft
delete (move to Deleted Items) is reversible and ungated.
"""
from __future__ import annotations

from typing import Any, Dict, List

from .base import BaseTool
from ..exceptions import ToolExecutionError, ValidationError
from ..utils import (
    format_success_response,
    safe_get,
    find_message_for_account,
    ews_id_to_str,
)
from ..safety import make_token, verify_token, content_hash
from .email_tools import resolve_folder_for_account, FLAG_STATUS_MAP

_MAX_IDS = 50


def _validate_ids(message_ids: Any) -> List[str]:
    if not message_ids or not isinstance(message_ids, list):
        raise ValidationError("message_ids must be a non-empty array of message ids")
    ids = [m for m in message_ids if isinstance(m, str) and m.strip()]
    if not ids:
        raise ValidationError("message_ids must contain at least one non-empty id")
    if len(ids) > _MAX_IDS:
        raise ValidationError(f"too many ids: {len(ids)} > {_MAX_IDS} (page your batch)")
    return ids


def _summary(results: List[Dict[str, Any]]) -> Dict[str, int]:
    ok = sum(1 for r in results if r.get("ok"))
    return {"count": len(results), "ok_count": ok, "error_count": len(results) - ok}


class UpdateMessagesTool(BaseTool):
    """Bulk mark read/unread, (re)categorize, flag, or set importance."""

    side_effect_class = "write"

    def get_schema(self) -> Dict[str, Any]:
        return {
            "name": "update_messages",
            "description": (
                "Bulk-update up to 50 messages: read/unread, categories, flag "
                "status, importance. Returns per-item results (partial success "
                "allowed). For a single message, update_email also works."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "message_ids": {"type": "array", "items": {"type": "string"}, "description": "Message ids to update (max 50)"},
                    "is_read": {"type": "boolean"},
                    "categories": {"type": "array", "items": {"type": "string"}},
                    "flag_status": {"type": "string", "enum": ["NotFlagged", "Flagged", "Complete"]},
                    "importance": {"type": "string", "enum": ["Low", "Normal", "High"]},
                    "target_mailbox": {"type": "string"},
                },
                "required": ["message_ids"],
            },
        }

    async def execute(self, **kwargs) -> Dict[str, Any]:
        ids = _validate_ids(kwargs.get("message_ids"))
        target_mailbox = kwargs.get("target_mailbox")

        # Validate flag_status once (request-level, not per item).
        flag_value = None
        if "flag_status" in kwargs and kwargs["flag_status"] is not None:
            if kwargs["flag_status"] not in FLAG_STATUS_MAP:
                raise ToolExecutionError(
                    f"Invalid flag_status: {kwargs['flag_status']}. "
                    f"Valid: {', '.join(FLAG_STATUS_MAP.keys())}"
                )
            flag_value = FLAG_STATUS_MAP[kwargs["flag_status"]]

        if not any(k in kwargs and kwargs[k] is not None for k in ("is_read", "categories", "flag_status", "importance")):
            raise ToolExecutionError("Provide at least one of: is_read, categories, flag_status, importance.")

        account = self.get_account(target_mailbox)
        mailbox = self.get_mailbox_info(target_mailbox)

        results: List[Dict[str, Any]] = []
        for mid in ids:
            try:
                message = find_message_for_account(account, mid)
                updates: Dict[str, Any] = {}
                if kwargs.get("is_read") is not None:
                    message.is_read = kwargs["is_read"]
                    updates["is_read"] = kwargs["is_read"]
                if kwargs.get("categories") is not None:
                    message.categories = kwargs["categories"]
                    updates["categories"] = kwargs["categories"]
                if kwargs.get("flag_status") is not None:
                    message.flag_status_value = flag_value
                    updates["flag_status"] = kwargs["flag_status"]
                if kwargs.get("importance") is not None:
                    message.importance = kwargs["importance"]
                    updates["importance"] = kwargs["importance"]
                message.save()
                results.append({"message_id": mid, "ok": True, "updates": updates})
            except Exception as e:
                results.append({"message_id": mid, "ok": False, "error": f"{type(e).__name__}: {e}"})

        return format_success_response("Bulk update complete", results=results, mailbox=mailbox, **_summary(results))


class MoveMessagesTool(BaseTool):
    """Bulk move messages to a folder; returns the new id for each."""

    side_effect_class = "write"

    def get_schema(self) -> Dict[str, Any]:
        return {
            "name": "move_messages",
            "description": (
                "Bulk-move up to 50 messages to a folder (by name/path or id). "
                "Returns per-item results including the NEW message id (EWS ids "
                "change on move — use the returned new_id afterwards)."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "message_ids": {"type": "array", "items": {"type": "string"}, "description": "Message ids to move (max 50)"},
                    "destination_folder": {"type": "string", "description": "Destination folder name or path"},
                    "destination_folder_id": {"type": "string", "description": "Destination folder id (alternative to destination_folder)"},
                    "target_mailbox": {"type": "string"},
                },
                "required": ["message_ids"],
            },
        }

    async def execute(self, **kwargs) -> Dict[str, Any]:
        ids = _validate_ids(kwargs.get("message_ids"))
        destination_folder = kwargs.get("destination_folder")
        destination_folder_id = kwargs.get("destination_folder_id")
        if not destination_folder and not destination_folder_id:
            raise ToolExecutionError("Either destination_folder or destination_folder_id is required")
        target_mailbox = kwargs.get("target_mailbox")

        account = self.get_account(target_mailbox)
        mailbox = self.get_mailbox_info(target_mailbox)

        # Resolve the destination once for the whole batch.
        destination_identifier = destination_folder_id or destination_folder
        dest_folder = await resolve_folder_for_account(account, destination_identifier)
        dest_name = safe_get(dest_folder, "name", destination_identifier)

        results: List[Dict[str, Any]] = []
        for mid in ids:
            try:
                item = find_message_for_account(account, mid)
                item.move(dest_folder)
                results.append({"message_id": mid, "ok": True, "new_id": ews_id_to_str(safe_get(item, "id", None))})
            except Exception as e:
                results.append({"message_id": mid, "ok": False, "error": f"{type(e).__name__}: {e}"})

        return format_success_response(
            f"Bulk move to {dest_name} complete", results=results, destination_folder=dest_name,
            mailbox=mailbox, **_summary(results),
        )


class DeleteMessagesTool(BaseTool):
    """Bulk delete messages — soft by default; hard delete is two-phase."""

    side_effect_class = "destructive"

    def get_schema(self) -> Dict[str, Any]:
        return {
            "name": "delete_messages",
            "description": (
                "Bulk-delete up to 50 messages. Soft delete (default) moves to "
                "Deleted Items and is reversible. permanent=true is a HARD "
                "delete and is two-phase: call once without confirm_token to get "
                "a preview + token, then again with the token to delete."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "message_ids": {"type": "array", "items": {"type": "string"}, "description": "Message ids to delete (max 50)"},
                    "permanent": {"type": "boolean", "default": False, "description": "Hard delete (bypasses Deleted Items). Two-phase. Alias: hard_delete."},
                    "hard_delete": {"type": "boolean", "default": False, "description": "Alias for permanent."},
                    "confirm_token": {"type": "string", "description": "Phase-2 token for hard delete."},
                    "target_mailbox": {"type": "string"},
                },
                "required": ["message_ids"],
            },
        }

    async def execute(self, **kwargs) -> Dict[str, Any]:
        ids = _validate_ids(kwargs.get("message_ids"))
        permanent = bool(kwargs.get("permanent", False) or kwargs.get("hard_delete", False))
        confirm_token = kwargs.get("confirm_token")
        target_mailbox = kwargs.get("target_mailbox")

        account = self.get_account(target_mailbox)
        mailbox = self.get_mailbox_info(target_mailbox)

        # Hard delete is irreversible → two-phase, bound to this exact id set.
        if permanent:
            chash = content_hash(sorted(ids))
            if not confirm_token:
                tok = make_token(mailbox=mailbox, action="delete_messages", target_id=mailbox, chash=chash)
                return format_success_response(
                    "Confirmation required for permanent delete — review, then call again with confirm_token.",
                    requires_confirmation=True, confirm_token=tok["confirm_token"],
                    expires_at=tok["expires_at"], ttl_seconds=tok["ttl_seconds"],
                    preview={"permanent": True, "message_ids": ids, "count": len(ids)},
                    mailbox=mailbox,
                )
            ok, reason = verify_token(confirm_token, mailbox=mailbox, action="delete_messages", target_id=mailbox, chash=chash)
            if not ok:
                hint = {
                    "expired": "Token expired — re-request without a token.",
                    "stale": "The id set changed since preview — re-request to get a new token.",
                }.get(reason, "Invalid confirmation token.")
                raise ToolExecutionError(f"Confirmation failed ({reason}): {hint}")

        results: List[Dict[str, Any]] = []
        for mid in ids:
            try:
                item = find_message_for_account(account, mid)
                if permanent:
                    item.delete()  # exchangelib 5.x: bare delete() is HARD_DELETE
                    action = "permanently_deleted"
                else:
                    item.move(account.trash)
                    action = "moved_to_trash"
                results.append({"message_id": mid, "ok": True, "action": action})
            except Exception as e:
                results.append({"message_id": mid, "ok": False, "error": f"{type(e).__name__}: {e}"})

        return format_success_response(
            "Bulk delete complete", results=results, permanent=permanent, mailbox=mailbox, **_summary(results),
        )
