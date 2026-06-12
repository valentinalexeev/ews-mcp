"""Base class for all MCP tools."""

import asyncio
import fnmatch
import os
import threading
from collections import deque
from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, Type, Optional
from pydantic import BaseModel, ValidationError as PydanticValidationError
from exchangelib import Account
import logging
import time

from ..ews_client import EWSClient
from ..exceptions import ValidationError, ToolExecutionError
from ..id_alias import get_aliaser, kind_for_key
from ..safety import content_hash, make_token, verify_token
from ..utils import format_error_response
from ..logging_system import get_logger
from ..middleware.circuit_breaker import get_circuit_breaker

# Capability tiers: read ⊂ draft ⊂ full. The configured tier
# (EWS_CAPABILITY_TIER, default "full" = no behavior change) caps which
# side-effect classes may run: read tools everywhere, reversible writes
# from "draft", sends and destructive ops only in "full".
_TIER_RANK = {"read": 0, "draft": 1, "full": 2}
_CLASS_REQUIRED_TIER = {
    "read": "read", "write": "draft", "destructive": "full", "send": "full",
}

# Recipient-bearing kwargs scanned by the confirm preview's
# external-recipient warning.
_RECIPIENT_KEYS = ("to", "cc", "bcc", "attendees", "required_attendees",
                   "optional_attendees")

# Send rate cap: timestamps of recent send-class executions, process-wide.
_SEND_TIMES: deque = deque()
_SEND_TIMES_LOCK = threading.Lock()
_SEND_WINDOW_SECONDS = 3600


def _split_patterns(raw: str) -> list:
    return [p.strip().lower() for p in (raw or "").split(",") if p.strip()]


def reset_send_rate_window() -> None:
    """Test hook: clear the process-wide send-rate window."""
    with _SEND_TIMES_LOCK:
        _SEND_TIMES.clear()


# Injected into the published schema of every confirm-gated tool so the
# model knows the two-phase contract without per-tool schema edits.
CONFIRM_TOKEN_PROPERTY = {
    "type": "string",
    "description": (
        "Two-phase confirm: omit on the first call — you get a preview "
        "plus a confirm_token and NOTHING executes. Call again with "
        "identical arguments plus this token to actually execute. Tokens "
        "are single-purpose, bound to the exact arguments, and expire."
    ),
}


def tool_uses_confirm(tool: "BaseTool") -> bool:
    """True when the central two-phase gate can fire for this tool."""
    return bool(getattr(tool, "confirm_required", False)) or (
        type(tool).confirm_needed is not BaseTool.confirm_needed
    )


def public_schema(tool: "BaseTool") -> Dict[str, Any]:
    """The tool's schema as published to clients (MCP list_tools, OpenAPI),
    with the confirm_token parameter injected for confirm-gated tools."""
    schema = tool.get_schema()
    if tool_uses_confirm(tool):
        props = schema.get("inputSchema", {}).setdefault("properties", {})
        props.setdefault("confirm_token", dict(CONFIRM_TOKEN_PROPERTY))
    return schema

# ---------------------------------------------------------------------------
# Central id aliasing (token economy + move-stability)
#
# Raw EWS ItemIds are ~100-150 case-sensitive chars and change when an item
# moves folders. The dispatcher below (a) accepts short aliases anywhere a
# tool takes an id, (b) attaches a short ``sid`` next to every long id it
# ships out, (c) rebinds aliases when move responses report id remaps. Raw
# ids keep flowing unchanged — aliasing is strictly additive.
# ---------------------------------------------------------------------------

# Long EWS ids comfortably exceed this; UUIDs and friends stay untouched.
_ALIAS_MIN_ID_LEN = 40
_PRIMARY_ID_KEYS = frozenset({
    "message_id", "draft_id", "event_id", "item_id", "appointment_id",
    "task_id", "contact_id", "email_id",
})
_THREAD_ID_KEYS = frozenset({"conversation_id", "thread_id"})
_ID_LIST_KEYS = frozenset({"ids", "message_ids"})


def _resolve_alias_inputs(aliaser, kwargs: Dict[str, Any]) -> Dict[str, Any]:
    """Translate alias-shaped id params back to raw EWS ids. Raises KeyError
    (from ``IdAliaser.resolve``) for stale/unknown aliases."""
    out = dict(kwargs)
    for key, value in kwargs.items():
        if (key in _PRIMARY_ID_KEYS or key in _THREAD_ID_KEYS) and isinstance(value, str):
            out[key] = aliaser.resolve(value)
        elif key in _ID_LIST_KEYS and isinstance(value, list):
            out[key] = [
                aliaser.resolve(v) if isinstance(v, str) else v for v in value
            ]
    return out


def _alias_outputs(aliaser, node: Any) -> Any:
    """Walk a result structure, attaching ``sid`` next to long ids (in
    place) and rebinding aliases on move remaps ({old_id, new_id})."""
    if isinstance(node, dict):
        old_id, new_id = node.get("old_id"), node.get("new_id")
        if (
            isinstance(old_id, str) and isinstance(new_id, str)
            and len(new_id) >= _ALIAS_MIN_ID_LEN
        ):
            alias = aliaser.rebind(old_id, new_id)
            if alias:
                node.setdefault("sid", alias)
        imid = node.get("internet_message_id")
        if not isinstance(imid, str):
            imid = None
        for key in list(node.keys()):
            value = node[key]
            if isinstance(value, (dict, list)):
                _alias_outputs(aliaser, value)
            elif (
                key in _PRIMARY_ID_KEYS
                and isinstance(value, str)
                and len(value) >= _ALIAS_MIN_ID_LEN
            ):
                node.setdefault(
                    "sid",
                    aliaser.alias_for(
                        value, kind_for_key(key), internet_message_id=imid
                    ),
                )
            elif (
                key in _THREAD_ID_KEYS
                and isinstance(value, str)
                and len(value) >= _ALIAS_MIN_ID_LEN
            ):
                node.setdefault("thread_sid", aliaser.alias_for(value, "t"))
    elif isinstance(node, list):
        for item in node:
            _alias_outputs(aliaser, item)
    return node

# Dedicated bounded executor for EWS-touching tool bodies. exchangelib is
# synchronous: running it inline in async handlers froze the whole event
# loop (all concurrent requests, SSE keepalives, /health) for the duration
# of every Exchange round-trip. Tool bodies now run on this pool — its
# size doubles as the EWS concurrency cap, keeping us a polite guest on
# the mailbox's shared throttling budget (EWSMaxConcurrency is per-user
# and shared with the executive's own Outlook/OWA).
_EWS_EXECUTOR: Optional[ThreadPoolExecutor] = None
_EWS_EXECUTOR_LOCK = threading.Lock()


def _get_ews_executor(max_workers: int) -> ThreadPoolExecutor:
    global _EWS_EXECUTOR
    with _EWS_EXECUTOR_LOCK:
        if _EWS_EXECUTOR is None:
            _EWS_EXECUTOR = ThreadPoolExecutor(
                max_workers=max_workers, thread_name_prefix="ews-tool"
            )
        return _EWS_EXECUTOR


class BaseTool(ABC):
    """Base class for all MCP tools with integrated logging."""

    # Side-effect classification consumed by the central safety gate in
    # ``safe_execute``. Tools whose action leaves the trust boundary set
    # ``"send"`` and are refused when SEND_ENABLED=false. Subclasses override;
    # the default is the conservative ``"write"``.
    side_effect_class: str = "write"

    # Whether this tool needs a live Exchange connection. Tools that only
    # touch local state (whoami config mode, memory store, approval queue
    # bookkeeping) override with False so they keep working while the
    # background warmup is still trying to reach Exchange.
    requires_ews: bool = True

    # Two-phase confirm: when True (or when ``confirm_needed`` says so for
    # the given kwargs), the first call returns a preview + HMAC token and
    # performs NO side effect; the side effect runs only on a second call
    # carrying the (param-bound, TTL'd) token. send_draft keeps its own
    # richer internal flow and stays False here.
    confirm_required: bool = False

    def __init__(self, ews_client: EWSClient):
        self.ews_client = ews_client
        self.logger = logging.getLogger(self.__class__.__name__)
        self.log_manager = get_logger()

    @abstractmethod
    def get_schema(self) -> Dict[str, Any]:
        """Return tool schema for MCP registration."""
        pass

    @abstractmethod
    async def execute(self, **kwargs) -> Dict[str, Any]:
        """Execute tool operation."""
        pass

    def validate_input(self, model: Type[BaseModel], **kwargs) -> BaseModel:
        """Validate input using Pydantic model. Returns human-readable errors."""
        try:
            return model(**kwargs)
        except PydanticValidationError as e:
            # Simplify Pydantic errors to single actionable lines
            errors = e.errors()
            messages = []
            for err in errors[:3]:  # Max 3 errors to keep it short
                field = ".".join(str(loc) for loc in err["loc"])
                msg = err["msg"]
                if "Value error, " in msg:
                    msg = msg.replace("Value error, ", "")
                messages.append(f"{field}: {msg}")
            short_msg = "; ".join(messages)
            self.logger.error(f"Validation: {short_msg}")
            raise ValidationError(short_msg)

    def get_account(self, target_mailbox: Optional[str] = None) -> Account:
        """Get Exchange account for operations."""
        return self.ews_client.get_account(target_mailbox)

    def get_mailbox_info(self, target_mailbox: Optional[str] = None) -> str:
        """Get mailbox identifier for response."""
        if target_mailbox and target_mailbox.lower() != self.ews_client.config.ews_email.lower():
            return target_mailbox
        return self.ews_client.config.ews_email

    def confirm_needed(self, kwargs: Dict[str, Any]) -> bool:
        """Whether THIS invocation needs the two-phase confirm. Tools with
        conditional danger (hard delete, folder delete, OOF set) override."""
        return self.confirm_required

    def confirm_preview(self, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        """Human/model-readable preview for phase 1. Bodies are shown as a
        200-char snippet — enough to confirm intent without re-shipping the
        full content."""
        preview: Dict[str, Any] = {}
        for key, value in kwargs.items():
            if key in ("body", "message", "comment") and isinstance(value, str):
                preview[f"{key}_preview"] = value[:200]
            else:
                preview[key] = value
        return preview

    def _external_recipients(self, kwargs: Dict[str, Any]) -> list:
        """Recipients outside the primary mailbox's domain (for warnings)."""
        own_domain = (self.ews_client.config.ews_email or "").rsplit("@", 1)[-1].lower()
        external = []
        for key in _RECIPIENT_KEYS:
            value = kwargs.get(key)
            if isinstance(value, str):
                value = [value]
            if not isinstance(value, list):
                continue
            for recipient in value:
                if (
                    isinstance(recipient, str) and "@" in recipient
                    and recipient.rsplit("@", 1)[-1].lower() != own_domain
                ):
                    external.append(recipient)
        return external

    def _gather_recipients(self, kwargs: Dict[str, Any]) -> list:
        recipients = []
        for key in _RECIPIENT_KEYS:
            value = kwargs.get(key)
            if isinstance(value, str):
                value = [value]
            if isinstance(value, list):
                recipients.extend(
                    r.lower() for r in value if isinstance(r, str) and "@" in r
                )
        return recipients

    def _recipient_guard(self, kwargs: Dict[str, Any]) -> Optional[str]:
        """Allowlist/denylist check over kwargs-visible recipients.

        Returns a refusal message, or None when clear. Patterns are
        comma-separated fnmatch globs (e.g. '*@example.com'). Limitation:
        only recipients visible in the call's arguments are checked —
        send_draft recipients live in the draft itself.
        """
        cfg = self.ews_client.config
        denylist = _split_patterns(getattr(cfg, "ews_recipient_denylist", "") or "")
        allowlist = _split_patterns(getattr(cfg, "ews_recipient_allowlist", "") or "")
        if not denylist and not allowlist:
            return None
        recipients = self._gather_recipients(kwargs)
        for recipient in recipients:
            if any(fnmatch.fnmatch(recipient, p) for p in denylist):
                return (
                    f"recipient '{recipient}' is denylisted on this server "
                    "(EWS_RECIPIENT_DENYLIST)."
                )
        if allowlist:
            for recipient in recipients:
                if not any(fnmatch.fnmatch(recipient, p) for p in allowlist):
                    return (
                        f"recipient '{recipient}' is not on this server's "
                        "allowlist (EWS_RECIPIENT_ALLOWLIST)."
                    )
        return None

    def _send_rate_guard(self) -> Optional[str]:
        """Sends-per-hour cap across all send-class tools. Returns a
        refusal message when the window is full, else records this send."""
        try:
            cap = int(getattr(self.ews_client.config, "ews_max_sends_per_hour", 0))
        except (TypeError, ValueError):
            cap = 0
        if cap <= 0:
            return None
        now = time.time()
        with _SEND_TIMES_LOCK:
            while _SEND_TIMES and now - _SEND_TIMES[0] > _SEND_WINDOW_SECONDS:
                _SEND_TIMES.popleft()
            if len(_SEND_TIMES) >= cap:
                retry_in = int(_SEND_WINDOW_SECONDS - (now - _SEND_TIMES[0]))
                return (
                    f"send rate cap reached ({cap}/hour, "
                    f"EWS_MAX_SENDS_PER_HOUR). Window frees in ~{retry_in}s."
                )
            _SEND_TIMES.append(now)
        return None

    def mint_confirm_token(self, kwargs: Dict[str, Any]) -> str:
        """Mint a valid phase-2 token for these exact kwargs.

        Used by the approval-queue executor: a human already approved the
        action, so the executor pre-confirms through the same HMAC
        machinery instead of a forgeable bypass flag.
        """
        chash = content_hash(dict(kwargs))
        tok = make_token(
            mailbox=self.ews_client.config.ews_email,
            action=self.get_schema()["name"],
            target_id="-",
            chash=chash,
        )
        return tok["confirm_token"]

    def _confirm_gate(self, tool_name: str, kwargs: Dict[str, Any]):
        """Two-phase confirm. Returns (kwargs, None) to proceed, or
        (kwargs, response) to short-circuit with phase-1 preview/rejection.

        The content hash binds the token to the exact pre-alias-resolution
        kwargs (minus the token itself): any change between the previewed
        phase 1 and phase 2 — or a token minted for a different tool —
        verifies as stale.
        """
        kwargs = dict(kwargs)
        token = kwargs.pop("confirm_token", None)
        chash = content_hash(dict(kwargs))
        mailbox = self.ews_client.config.ews_email
        if not token:
            tok = make_token(
                mailbox=mailbox, action=tool_name, target_id="-", chash=chash,
            )
            response: Dict[str, Any] = {
                "success": True,
                "requires_confirmation": True,
                "message": (
                    f"{tool_name} previewed — NOTHING was executed. Review "
                    "the preview, then call again with the same arguments "
                    "plus confirm_token to proceed."
                ),
                "preview": self.confirm_preview(kwargs),
                **tok,
            }
            external = self._external_recipients(kwargs)
            if external:
                response["warnings"] = [
                    f"external recipients: {', '.join(sorted(set(external)))}"
                ]
            return kwargs, response
        ok, reason = verify_token(
            token, mailbox=mailbox, action=tool_name, target_id="-", chash=chash,
        )
        if not ok:
            err = ValidationError(
                f"confirm_token rejected ({reason}). Call {tool_name} again "
                "WITHOUT confirm_token to get a fresh preview and token; "
                "tokens die when any argument changes or after the TTL."
            )
            return kwargs, format_error_response(err, "")
        return kwargs, None

    def _get_aliaser(self):
        """Process-wide id aliaser, or None when disabled/unavailable.

        Aliasing must never take a tool call down — any setup failure
        degrades to raw-id passthrough.
        """
        if not bool(getattr(self.ews_client.config, "ews_id_aliases_enabled", True)):
            return None
        try:
            return get_aliaser(os.environ.get("EWS_MEMORY_DIR", "data/memory"))
        except Exception as exc:
            self.logger.debug(f"id aliaser unavailable: {exc}")
            return None

    def get_memory_store(self):
        """Return the persistent memory store for the primary authenticated mailbox.

        Agentic features (commitments, approvals, rules, voice profile, OOF
        policy) are intentionally stored against the *primary* mailbox, not
        the impersonated ``target_mailbox``. The primary mailbox is the
        operator whose server is running; per-target state would either leak
        across service-account boundaries or require a per-target-mailbox DB
        each call, which we don't want yet.
        """
        from ..memory import MemoryStore
        return MemoryStore.for_mailbox(self.ews_client.config.ews_email)

    async def safe_execute(self, **kwargs) -> Dict[str, Any]:
        """Execute with error handling, circuit breaker, and logging."""
        start_time = time.time()
        tool_name = self.get_schema()["name"]
        module_name = self.__class__.__module__.split('.')[-1]

        # Circuit breaker check
        cb = get_circuit_breaker()
        try:
            cb.check()
        except ToolExecutionError as e:
            return format_error_response(e, "")

        # Cold-start gate: while the background warmup has never reached
        # Exchange, EWS-touching tools fail fast with a retry hint instead
        # of burning a thread on a connection that isn't there yet. Local
        # tools (requires_ews=False) pass through. A *degraded* (was-warm)
        # connection still attempts the call — the circuit breaker owns
        # repeated-failure protection.
        manager = getattr(self.ews_client, "connection_manager", None)
        if (
            self.requires_ews
            and manager is not None
            and manager.state == "connecting"
        ):
            status = manager.status()
            retry_in = status.get("next_retry_in_s")
            err = ToolExecutionError(
                f"{tool_name} unavailable: the Exchange connection is still "
                f"warming up (attempt {status.get('attempts', 0)}; last error: "
                f"{status.get('last_error') or 'none yet'}). Retry "
                f"{'in ~' + str(retry_in) + 's' if retry_in is not None else 'shortly'}, "
                "or call whoami with probe_connection=false for server status."
            )
            self._log_error(
                "UpstreamUnavailable", tool_name, module_name, kwargs, err,
                int((time.time() - start_time) * 1000),
            )
            return format_error_response(err, "")

        # Global send kill switch: refuse trust-boundary-leaving operations
        # when SEND_ENABLED=false, before the attempt log and before
        # execute(), so nothing reaches Exchange. Covers the approval-executor
        # path too (ExecuteApprovedActionTool routes through safe_execute).
        if self.side_effect_class == "send" and not getattr(
            self.ews_client.config, "send_enabled", True
        ):
            duration_ms = int((time.time() - start_time) * 1000)
            err = ToolExecutionError(
                f"{tool_name} is blocked: SEND_ENABLED=false. Outbound sending "
                "is disabled on this server. Create a draft instead, or set "
                "SEND_ENABLED=true to allow sending."
            )
            self._log_error("SendDisabled", tool_name, module_name, kwargs, err, duration_ms)
            return format_error_response(err, "")

        # Capability tier gate: the configured tier caps which side-effect
        # classes may run at all (read ⊂ draft ⊂ full). Registration-time
        # filtering already hides above-tier tools; this is defense in
        # depth for direct dispatch paths.
        tier = str(getattr(self.ews_client.config, "ews_capability_tier", "full"))
        required = _CLASS_REQUIRED_TIER.get(self.side_effect_class, "draft")
        if _TIER_RANK[required] > _TIER_RANK.get(tier, _TIER_RANK["full"]):
            duration_ms = int((time.time() - start_time) * 1000)
            err = ToolExecutionError(
                f"{tool_name} is blocked: this server runs in capability "
                f"tier '{tier}' and {self.side_effect_class}-class tools "
                f"need tier '{required}'. (EWS_CAPABILITY_TIER governs this.)"
            )
            self._log_error("TierBlocked", tool_name, module_name, kwargs, err, duration_ms)
            return format_error_response(err, "")

        # Recipient allowlist/denylist for send-class tools — refused
        # before even the phase-1 preview.
        if self.side_effect_class == "send":
            guard_msg = self._recipient_guard(kwargs)
            if guard_msg is not None:
                duration_ms = int((time.time() - start_time) * 1000)
                err = ToolExecutionError(f"{tool_name} refused: {guard_msg}")
                self._log_error(
                    "RecipientBlocked", tool_name, module_name, kwargs, err, duration_ms
                )
                return format_error_response(err, "")

        # Two-phase confirm for irreversible/externally-visible operations:
        # phase 1 (no token) previews and does nothing; phase 2 requires
        # the param-bound token. Runs BEFORE alias resolution so both
        # phases hash the caller's literal arguments.
        if self.confirm_needed(kwargs):
            kwargs, confirm_response = self._confirm_gate(tool_name, kwargs)
            if confirm_response is not None:
                return confirm_response

        # Send rate cap: this call WILL execute a send (confirm satisfied
        # or not required) — count it against the hourly window.
        if self.side_effect_class == "send":
            rate_msg = self._send_rate_guard()
            if rate_msg is not None:
                duration_ms = int((time.time() - start_time) * 1000)
                err = ToolExecutionError(f"{tool_name} refused: {rate_msg}")
                self._log_error(
                    "SendRateCapped", tool_name, module_name, kwargs, err, duration_ms
                )
                return format_error_response(err, "")

        # Alias resolution: accept short ids (m12, e3, ...) anywhere a
        # tool takes an id. A stale alias is a clean user error with a
        # re-search hint, not a 500.
        aliaser = self._get_aliaser()
        if aliaser is not None:
            try:
                kwargs = _resolve_alias_inputs(aliaser, kwargs)
            except KeyError as e:
                duration_ms = int((time.time() - start_time) * 1000)
                err = ValidationError(e.args[0] if e.args else str(e))
                self._log_error(
                    "StaleAlias", tool_name, module_name, kwargs, err, duration_ms
                )
                return format_error_response(err, "")

        # Log attempt
        self.log_manager.log_activity(
            level="INFO",
            module=module_name,
            action=f"{tool_name.upper()}_ATTEMPT",
            data=self._sanitize_kwargs(kwargs),
            result={"status": "attempting"},
            context={"tool": tool_name}
        )

        try:
            result = await self._run_execute(**kwargs)
            duration_ms = int((time.time() - start_time) * 1000)

            # Attach short aliases next to long EWS ids (additive; never
            # allowed to break a successful response).
            if aliaser is not None and isinstance(result, dict):
                try:
                    _alias_outputs(aliaser, result)
                except Exception as alias_exc:
                    self.logger.warning(f"id aliasing skipped: {alias_exc}")

            # Record success with circuit breaker
            cb.record_success()

            # Log success
            self.log_manager.log_activity(
                level="INFO",
                module=module_name,
                action=f"{tool_name.upper()}_SUCCESS",
                data=self._sanitize_kwargs(kwargs),
                result={
                    "status": "success",
                    "duration_ms": duration_ms,
                    **result
                },
                context={"tool": tool_name}
            )

            # Log performance
            self.log_manager.log_performance(
                metric="api_call",
                tool=tool_name,
                duration_ms=duration_ms,
                status="success"
            )

            # Audit log
            self.log_manager.log_audit(
                user=self.ews_client.config.ews_email,
                action=tool_name,
                resource=f"{tool_name}_operation",
                result="success",
                details={"duration_ms": duration_ms}
            )

            return result

        except ValidationError as e:
            duration_ms = int((time.time() - start_time) * 1000)
            # Validation errors are user errors — don't trip circuit breaker
            self._log_error("ValidationError", tool_name, module_name, kwargs, e, duration_ms)
            return format_error_response(e, "")

        except ToolExecutionError as e:
            duration_ms = int((time.time() - start_time) * 1000)
            error_str = str(e).lower()
            # Only trip circuit breaker for connectivity/server errors
            if any(kw in error_str for kw in ["connect", "timeout", "unavailable", "transport"]):
                cb.record_failure()
            self._log_error("ToolExecutionError", tool_name, module_name, kwargs, e, duration_ms)
            return format_error_response(e, "")

        except Exception as e:
            duration_ms = int((time.time() - start_time) * 1000)
            cb.record_failure()
            self.logger.exception("Unexpected error in tool execution")
            self._log_error("UnexpectedError", tool_name, module_name, kwargs, e, duration_ms)
            return format_error_response(e, "Unexpected error")

    async def _run_execute(self, **kwargs) -> Dict[str, Any]:
        """Run ``execute()`` — off the event loop for EWS-touching tools.

        The tool body (an async def whose awaits are cheap but whose
        exchangelib calls block) runs under ``asyncio.run`` on a bounded
        worker thread, so one slow Exchange round-trip can't freeze every
        other request. Local tools (requires_ews=False) and deployments
        that set EWS_OFFLOAD_ENABLED=false keep the historical inline
        path. Caveat: cancellation of the outer task does not interrupt
        an in-flight worker call — exchangelib offers no cancel anyway.
        """
        cfg = self.ews_client.config
        offload = self.requires_ews and bool(
            getattr(cfg, "ews_offload_enabled", True)
        )
        if not offload:
            return await self.execute(**kwargs)
        try:
            max_workers = int(getattr(cfg, "ews_max_concurrency", 4))
        except (TypeError, ValueError):
            max_workers = 4
        loop = asyncio.get_running_loop()
        executor = _get_ews_executor(max_workers)
        return await loop.run_in_executor(
            executor, lambda: asyncio.run(self.execute(**kwargs))
        )

    def _log_error(self, error_type: str, tool_name: str, module_name: str,
                   kwargs: Dict, error: Exception, duration_ms: int):
        """Log error with full context."""
        self.log_manager.log_activity(
            level="ERROR",
            module=module_name,
            action=f"{tool_name.upper()}_ERROR",
            data=self._sanitize_kwargs(kwargs),
            result={
                "status": "failed",
                "error": str(error),
                "error_type": error_type,
                "duration_ms": duration_ms
            },
            context={"tool": tool_name}
        )

        self.log_manager.log_performance(
            metric="api_call",
            tool=tool_name,
            duration_ms=duration_ms,
            status="failed",
            error_type=error_type
        )

        self.log_manager.log_audit(
            user=self.ews_client.config.ews_email,
            action=tool_name,
            resource=f"{tool_name}_operation",
            result="failed",
            details={"error": str(error), "error_type": error_type}
        )

    def _sanitize_kwargs(self, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        """Sanitize kwargs for logging (remove sensitive data)."""
        sanitized = {}
        sensitive_keys = ['password', 'secret', 'token', 'api_key', 'body', 'content']

        for key, value in kwargs.items():
            if any(sensitive in key.lower() for sensitive in sensitive_keys):
                if isinstance(value, str) and len(value) > 50:
                    sanitized[key] = f"{value[:50]}... (truncated)"
                else:
                    sanitized[key] = "***REDACTED***"
            else:
                sanitized[key] = value

        return sanitized
