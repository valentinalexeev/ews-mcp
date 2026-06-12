#!/usr/bin/env python3
"""Local boot smoke: prove the never-exit startup end-to-end.

Boots the real HTTP server with an UNREACHABLE Exchange endpoint, then
asserts:
  * the process survives (no exit-on-failed-connection),
  * /livez = 200, /health = 200 with a full tool count,
  * /readyz = 503 with state=connecting + attempts/next_retry detail,
  * the REST shim answers (whoami works cold; an EWS tool returns the
    upstream-unavailable hint instead of hanging or crashing),
  * /openapi.json includes confirm_token for send_email,
  * tier filtering works when EWS_CAPABILITY_TIER=draft.

Run: python scripts/local_boot_smoke.py   (exit 0 = all good)
"""
import json
import os
import subprocess
import sys
import time
import urllib.request
import urllib.error

PORT = 8123
BASE = f"http://127.0.0.1:{PORT}"
API_KEY = "smoke-test-key"


def _get(path, timeout=5):
    req = urllib.request.Request(BASE + path)
    req.add_header("Authorization", f"Bearer {API_KEY}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def _post(path, payload, timeout=20):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(BASE + path, data=data, method="POST")
    req.add_header("Authorization", f"Bearer {API_KEY}")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def main() -> int:
    env = dict(os.environ)
    env.update({
        "EWS_EMAIL": "smoke@example.invalid",
        "EWS_AUTH_TYPE": "basic",
        "EWS_USERNAME": "smoke",
        "EWS_PASSWORD": "smoke",
        "EWS_AUTODISCOVER": "false",
        # TEST-NET-1 address: connection attempts time out / refuse fast.
        "EWS_SERVER_URL": "https://192.0.2.1/EWS/Exchange.asmx",
        "MCP_TRANSPORT": "sse",
        "MCP_HOST": "127.0.0.1",
        "MCP_PORT": str(PORT),
        "MCP_API_KEY": API_KEY,
        "SEND_ENABLED": "false",
        "ENABLE_AI": "false",
        "REQUEST_TIMEOUT": "3",
        "LOG_LEVEL": "WARNING",
        "EWS_MEMORY_DIR": os.path.join(os.environ.get("TEMP", "/tmp"), "ews-smoke-mem"),
    })
    capability_tier = env.get("SMOKE_TIER")
    if capability_tier:
        env["EWS_CAPABILITY_TIER"] = capability_tier

    proc = subprocess.Popen(
        [sys.executable, "-m", "src.main"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    failures = []
    try:
        # Server must come up promptly even though Exchange is unreachable.
        deadline = time.time() + 30
        livez = None
        while time.time() < deadline:
            if proc.poll() is not None:
                print(f"FAIL: process exited early with {proc.returncode}")
                return 1
            try:
                livez = _get("/livez", timeout=2)
                break
            except Exception:
                time.sleep(0.5)
        if livez is None:
            print("FAIL: /livez never came up")
            return 1
        if livez[0] != 200:
            failures.append(f"/livez {livez}")

        status, health = _get("/health")
        if status != 200 or health.get("tools", 0) < 10:
            failures.append(f"/health {status} {health}")
        tool_count = health.get("tools", 0)

        status, ready = _get("/readyz")
        conn = ready.get("connection", {})
        if status != 503 or conn.get("state") != "connecting":
            failures.append(f"/readyz {status} {ready}")
        if conn.get("attempts", -1) < 0:
            failures.append(f"/readyz attempts missing: {ready}")

        # whoami works cold (requires_ews=False, probe off).
        status, who = _post("/api/tools/whoami", {"probe_connection": False})
        data = who.get("data", who)
        if status != 200 or not data.get("success"):
            failures.append(f"whoami cold {status} {who}")
        managed = (data.get("connection") or {}).get("managed") or {}
        if managed.get("state") not in ("connecting", "degraded", "warm"):
            failures.append(f"whoami managed state missing: {data.get('connection')}")

        # An EWS tool fails fast with the warming-up hint, not a hang/500.
        status, read = _post("/api/tools/read_emails", {"max_results": 1})
        rdata = read.get("data", read)
        if rdata.get("success") is not False or "warming up" not in str(rdata.get("error", "")):
            failures.append(f"read_emails cold {status} {read}")

        # OpenAPI publishes the confirm contract.
        status, openapi = _get("/openapi.json")
        send_email_path = openapi.get("paths", {}).get("/api/tools/send_email")
        if capability_tier == "draft":
            if send_email_path is not None:
                failures.append("tier=draft but send_email still in OpenAPI")
            if tool_count >= 70:
                failures.append(f"tier=draft but {tool_count} tools registered")
        else:
            body_schema = (
                (send_email_path or {}).get("post", {})
                .get("requestBody", {}).get("content", {})
                .get("application/json", {}).get("schema", {})
            )
            if "confirm_token" not in json.dumps(body_schema):
                failures.append("confirm_token absent from send_email OpenAPI schema")

        if failures:
            print("FAILURES:")
            for f in failures:
                print(" -", f)
            return 1
        print(f"OK: boot-without-Exchange smoke passed (tools={tool_count}, "
              f"tier={capability_tier or 'full'})")
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    sys.exit(main())
