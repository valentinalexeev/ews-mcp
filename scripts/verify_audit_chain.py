#!/usr/bin/env python3
"""Verify the audit log's hash chain.

Each AuditLogger record line ends with ``| seq=N | prev=<hash12> | h=<hash>``
where ``h = sha256(prev_full_hash | core)`` and ``core`` is the record text
up to and including the ``prev=`` field. The chain restarts at GENESIS
whenever ``seq=1`` (process start). Any edited or deleted record breaks
every hash after it within its run.

Usage:
    python scripts/verify_audit_chain.py [logs/audit.log]

Exit codes: 0 = chain intact, 1 = break detected, 2 = usage/IO error.
"""
import hashlib
import re
import sys
from pathlib import Path

# Formatter prefix: "YYYY-mm-dd HH:MM:SS | LEVEL | <core> | h=<hash>"
_LINE = re.compile(
    r"^(?P<ts>[^|]+) \| (?P<level>[A-Z]+) \| (?P<core>.+ \| seq=(?P<seq>\d+) "
    r"\| prev=[0-9A-Za-z]+) \| h=(?P<h>[0-9a-f]{64})$"
)


def verify(path: Path) -> int:
    prev = "GENESIS"
    checked = 0
    breaks = 0
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        m = _LINE.match(raw.strip())
        if not m:
            continue  # pre-chain records or unrelated lines
        if m.group("seq") == "1":
            prev = "GENESIS"  # new process run
        expected = hashlib.sha256(
            f"{prev}|{m.group('core')}".encode("utf-8")
        ).hexdigest()
        if expected != m.group("h"):
            print(f"BREAK at line {lineno}: hash mismatch (seq={m.group('seq')})")
            breaks += 1
            # Re-anchor on the recorded hash so one break doesn't cascade.
        prev = m.group("h")
        checked += 1
    print(f"checked {checked} chained record(s); {breaks} break(s)")
    return 1 if breaks else 0


def main() -> int:
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("logs/audit.log")
    if not target.exists():
        print(f"no such file: {target}")
        return 2
    return verify(target)


if __name__ == "__main__":
    sys.exit(main())
