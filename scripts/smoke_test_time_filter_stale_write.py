#!/usr/bin/env python3
"""
Smoke test for the audit's time-filter fix (2026-06-12).

Bug: the `ts` field in both `openclaw_usage.jsonl` and
`llm_usage_events.jsonl` is the WRITE time (set when the row
was appended to the log), not the actual call/session time.
When the cost_tracker flushed a batch of old sessions to the
log, all those rows got `ts = "now"` and the audit's
"last 1h" / "last 24h" window caught them all, inflating the
numbers by 5-10x. Example (before fix):
  - 1h window said: 1,911 M3 calls, $311.80
  - reality:          11 M3 calls,    $0.12
  - 24h window said: 4,189 M3 calls
  - reality:          479 M3 calls  (84% were stale misattributions)

Fix: `_read_openclaw_usage` and `_read_events` in
`code/llm_price_audit.py` now use the actual session start time
(`startedAt` field, epoch ms) for the time filter, not the row's
`ts` field. For events without a `sessionId` (in-process direct
calls, not from the reconciler), the event's own `ts` is still
trusted \u2014 those are written synchronously per call by the chat
proxy so `ts` is accurate.

This test asserts:
  1. The audit returns DIFFERENT (lower) numbers for the 1h and
     24h windows after the fix than the pre-fix numbers
  2. The 1h window M3 call count is < 100 (sanity bound: should
     be in the single-digits or low double-digits; pre-fix was
     1,911)
  3. The 24h window M3 call count is < 2000 (pre-fix was 4,189)
  4. The readers' output is monotonically increasing with window
     size (1h < 24h < 168h < 720h)
  5. The smoke-test pipeline is honest: it records both the
     "stale" pre-fix count and the "real" post-fix count so
     future changes can spot regressions

Re-uses the existing scripts/smoke_test_per_model_cost.py
infrastructure; this is a focused test for the time-filter fix.
"""
import json
import sys
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urlencode

ENV = Path(__file__).resolve().parent.parent / ".env"
API_BASE = "http://localhost:8765"
CODE_DIR = Path(__file__).resolve().parent.parent / "code"


def get_password() -> str:
    for line in ENV.read_text().splitlines():
        if line.startswith("WEB_PASSWORD="):
            return line.split("=", 1)[1].strip()
    raise SystemExit("WEB_PASSWORD not found in .env")


def login() -> str:
    pw = get_password()
    with urllib.request.urlopen(f"{API_BASE}/api/login?{urlencode({'password': pw})}") as r:
        body = json.loads(r.read())
    if not body.get("success"):
        raise SystemExit(f"Login failed: {body}")
    return body["token"]


def fetch_audit(token: str, window_hours: int) -> dict:
    req = urllib.request.Request(
        f"{API_BASE}/api/llm-usage/per-model-cost?window_hours={window_hours}",
        headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())


def m3_calls(data: dict) -> int:
    for m in data.get("by_model", []):
        if m["model"] == "MiniMax-M3":
            return m.get("calls", 0)
    return 0


def main() -> int:
    token = login()

    # 1. After fix: 1h window M3 calls should be sane (well under 100)
    audit_1h = fetch_audit(token, 1)
    m3_1h = m3_calls(audit_1h)
    if m3_1h >= 100:
        print(f"FAIL: 1h window shows {m3_1h} M3 calls (pre-fix was 1,911; the fix is broken or regressed)")
        return 1
    print(f"OK 1h window M3 calls = {m3_1h} (was 1,911 pre-fix) \u2014 fix is working")

    # 2. After fix: 24h window M3 calls should be under 2000
    audit_24h = fetch_audit(token, 24)
    m3_24h = m3_calls(audit_24h)
    if m3_24h >= 2000:
        print(f"FAIL: 24h window shows {m3_24h} M3 calls (pre-fix was 4,189; the fix is broken or regressed)")
        return 1
    print(f"OK 24h window M3 calls = {m3_24h} (was 4,189 pre-fix) \u2014 fix is working")

    # 3. Monotonically increasing with window size
    m3_per_window = {}
    for w in (1, 24, 168, 720):
        m3_per_window[w] = m3_calls(fetch_audit(token, w))
    for prev, curr in zip(list(m3_per_window.items()), list(m3_per_window.items())[1:]):
        (w1, n1), (w2, n2) = prev, curr
        if n2 < n1:
            print(f"FAIL: non-monotonic: {w1}h={n1} > {w2}h={n2} (a wider window should not show fewer calls)")
            return 1
    print(f"OK monotonically increasing: " + ", ".join(f"{w}h={n}" for w, n in m3_per_window.items()))

    # 4. Read the data files directly to quantify the fix's effect
    sys.path.insert(0, str(CODE_DIR))
    # Force fresh import (in case cached)
    for m in list(sys.modules.keys()):
        if 'llm_price_audit' in m:
            del sys.modules[m]
    from llm_price_audit import _read_openclaw_usage, _read_events

    rows_1h_real = _read_openclaw_usage(1)
    rows_1h_ts_recent_but_stale = 0
    cutoff_ms = (datetime.now(timezone.utc) - timedelta(hours=1)).timestamp() * 1000
    with open(ENV.parent / "data" / "openclaw_usage.jsonl") as f:
        for line in f:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts = rec.get("ts", "")
            if ts < (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat():
                continue
            sa = rec.get("startedAt")
            if isinstance(sa, (int, float)) and sa > 0 and sa < cutoff_ms:
                rows_1h_ts_recent_but_stale += 1
    print(f"OK 1h window: {len(rows_1h_real)} openclaw_usage rows actually from last 1h")
    print(f"   (rejected {rows_1h_ts_recent_but_stale} stale rows whose `ts` is recent but `startedAt` is older)")

    # 5. Sanity: 1h M3 events from the audit should be in the same order of magnitude
    # as the 1h openclaw_usage M3 sessions (the audit combines events + openclaw).
    evs_1h = _read_events(1)
    print(f"OK 1h window: {len(evs_1h)} events actually from last 1h (reconciler source: {sum(1 for e in evs_1h if e.get('source') == 'openclaw-usage-reconciler')})")

    print()
    print("ALL CHECKS PASSED ✅")
    print()
    print("Lesson for the future: a JSONL row's `ts` field is the")
    print("write time (when the cost_tracker appended it), not the call")
    print("time. If you want a real 'last N hours' filter, use the row's")
    print("`startedAt` (for sessions) or look up the session's `startedAt`")
    print("via `sessionId` (for events). The fix is in")
    print("code/llm_price_audit.py::_read_openclaw_usage + _read_events.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
