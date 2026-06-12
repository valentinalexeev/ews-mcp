"""Hash-chained append-only audit log (IDEATION §9).

One JSONL record per tool call; ``hash = sha256(prev_hash | canonical)``.
Bodies never land here; recipients/subject do for send-class ops only.
"""

import hashlib
import json
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional


class AuditLog:
    def __init__(self, data_dir: str):
        self._dir = Path(data_dir) / "audit"
        self._lock = threading.Lock()
        self._prev = "GENESIS"
        self._seq = 0

    def record(
        self,
        tool: str,
        side_effect_class: str,
        outcome: str,
        latency_ms: int,
        transport: str = "-",
        detail: Optional[Dict[str, Any]] = None,
    ) -> None:
        entry: Dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "tool": tool,
            "class": side_effect_class,
            "outcome": outcome,
            "latency_ms": latency_ms,
            "transport": transport,
        }
        if detail:
            entry["detail"] = detail
        try:
            with self._lock:
                self._seq += 1
                entry["seq"] = self._seq
                entry["prev"] = self._prev[:12]
                canonical = json.dumps(entry, sort_keys=True, ensure_ascii=False)
                digest = hashlib.sha256(f"{self._prev}|{canonical}".encode()).hexdigest()
                entry["h"] = digest
                self._prev = digest
                self._dir.mkdir(parents=True, exist_ok=True)
                path = self._dir / f"audit-{time.strftime('%Y%m%d')}.jsonl"
                with path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(entry, sort_keys=True, ensure_ascii=False) + "\n")
        except Exception:
            # Auditing must never take a tool call down.
            pass
