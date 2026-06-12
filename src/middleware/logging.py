"""Enterprise-level structured logging configuration."""

import hashlib
import logging
import sys
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Dict


# Fields whose values must be redacted before being written to any log (audit
# or otherwise). Match is case-insensitive and substring-based so variants
# like "client_secret", "access_token", "auth_token" are covered.
_SENSITIVE_KEY_PATTERNS: tuple = (
    "password", "token", "secret", "api_key", "apikey", "authorization",
    # Mail content — bodies can contain PII, attachments are base64 blobs.
    "body", "html_body", "text_body",
    "file_content", "content_base64", "mime_content", "mime_content_base64",
    "inline_attachments",
)


def _is_sensitive(key: str) -> bool:
    # Normalise hyphens to underscores so header-style spellings
    # (`X-API-Key`, `Auth-Token`) match the same patterns as snake_case
    # field names (`api_key`, `auth_token`).
    lower = str(key).lower().replace("-", "_")
    return any(pattern in lower for pattern in _SENSITIVE_KEY_PATTERNS)


def redact_sensitive(obj: Any, max_str: int = 200) -> Any:
    """Return a copy of obj with sensitive fields replaced by "[redacted]".

    - Dict keys that match _SENSITIVE_KEY_PATTERNS are replaced whole-value.
    - Lists/tuples are walked recursively.
    - Long strings are truncated so a single audit line cannot grow unbounded.
    """
    if obj is None:
        return None
    if isinstance(obj, str):
        if len(obj) > max_str:
            return obj[: max_str - 3] + "..."
        return obj
    if isinstance(obj, (int, float, bool)):
        return obj
    if isinstance(obj, dict):
        redacted: Dict[str, Any] = {}
        for key, value in obj.items():
            if _is_sensitive(key):
                if isinstance(value, (list, tuple)):
                    redacted[key] = f"[redacted: {len(value)} item(s)]"
                elif isinstance(value, str):
                    redacted[key] = f"[redacted: {len(value)} chars]"
                else:
                    redacted[key] = "[redacted]"
            else:
                redacted[key] = redact_sensitive(value, max_str)
        return redacted
    if isinstance(obj, (list, tuple)):
        return [redact_sensitive(item, max_str) for item in obj]
    # Fallback: stringify and truncate.
    try:
        text = str(obj)
    except Exception:
        return "[non-serializable]"
    if len(text) > max_str:
        return text[: max_str - 3] + "..."
    return text


DEFAULT_LOG_DIR = Path("logs")
FALLBACK_LOG_DIR = Path("/tmp/ews_mcp_logs")


def resolve_log_dir(preferred: Path = DEFAULT_LOG_DIR) -> Path:
    """Return a writable log directory, falling back to /tmp when needed."""
    try:
        preferred.mkdir(parents=True, exist_ok=True)
        return preferred
    except OSError as e:
        fallback = FALLBACK_LOG_DIR
        logging.warning(f"Cannot create log directory {preferred}: {e}")
        fallback.mkdir(parents=True, exist_ok=True)
        logging.info(f"Using fallback log directory: {fallback}")
        return fallback


def setup_logging(log_level: str = "INFO") -> None:
    """Configure enterprise-level logging.

    - Console (stderr): Minimal monitoring info only
    - File (rotating): Complete troubleshooting logs
    - MCP requires stdout clean for JSON-RPC protocol
    """
    log_dir = resolve_log_dir()

    # Console handler: INFO level for monitoring
    console_handler = logging.StreamHandler(sys.stderr)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(logging.Formatter(
        '%(asctime)s [%(levelname)s] %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    ))

    # File handler: DEBUG level for troubleshooting (with rotation)
    file_handler = RotatingFileHandler(
        log_dir / "ews-mcp.log",
        maxBytes=10*1024*1024,  # 10MB
        backupCount=5
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - [%(filename)s:%(lineno)d] - %(message)s'
    ))

    # Error file handler: ERROR level for quick error review
    error_handler = RotatingFileHandler(
        log_dir / "ews-mcp-errors.log",
        maxBytes=10*1024*1024,  # 10MB
        backupCount=3
    )
    error_handler.setLevel(logging.ERROR)
    error_handler.setFormatter(logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - [%(filename)s:%(lineno)d] - %(message)s\n%(exc_info)s'
    ))

    # Configure root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, log_level.upper()))
    root_logger.addHandler(console_handler)
    root_logger.addHandler(file_handler)
    root_logger.addHandler(error_handler)

    # External library logging: WARNING to reduce noise
    logging.getLogger("exchangelib").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("requests").setLevel(logging.WARNING)
    logging.getLogger("requests_ntlm").setLevel(logging.WARNING)

    # Log startup
    logging.getLogger(__name__).info("Logging initialized: console=INFO, file=DEBUG, errors=ERROR")


class AuditLogger:
    """Enterprise audit logger for compliance and security.

    Records are hash-chained (CloudTrail-style, lightweight): each line
    carries ``seq``, a prefix of the previous record's hash, and
    ``h = sha256(prev_hash | core)``. Tampering with or deleting any
    record breaks every hash after it. The chain restarts at GENESIS on
    process start (seq=1). Verify with ``scripts/verify_audit_chain.py``.
    """

    def __init__(self):
        self.logger = logging.getLogger("audit")
        self._chain_lock = threading.Lock()
        self._prev_hash = "GENESIS"
        self._seq = 0

        # Add dedicated audit log file
        log_dir = resolve_log_dir()

        audit_handler = RotatingFileHandler(
            log_dir / "audit.log",
            maxBytes=20*1024*1024,  # 20MB
            backupCount=10  # Keep more audit history
        )
        audit_handler.setFormatter(logging.Formatter(
            '%(asctime)s | %(levelname)s | %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        ))
        self.logger.addHandler(audit_handler)
        self.logger.setLevel(logging.INFO)
        self.logger.propagate = False  # Don't duplicate to root logger

    def log_operation(
        self,
        operation: str,
        user: str,
        success: bool,
        details: Dict[str, Any] = None
    ) -> None:
        """Log operation for audit trail.

        Sensitive fields in `details` (passwords, tokens, email bodies,
        attachment bytes) are redacted before being written to audit.log.
        """
        message = f"op={operation} | user={user} | success={success}"
        if details:
            safe_details = redact_sensitive(details)
            message += f" | {safe_details}"

        with self._chain_lock:
            self._seq += 1
            core = f"{message} | seq={self._seq} | prev={self._prev_hash[:12]}"
            digest = hashlib.sha256(
                f"{self._prev_hash}|{core}".encode("utf-8")
            ).hexdigest()
            self._prev_hash = digest
            line = f"{core} | h={digest}"

        if success:
            self.logger.info(line)
        else:
            self.logger.warning(line)
