#!/usr/bin/env python3
"""
Unit test for the reconcile_openclaw_usage cache_read fix.

Bug: The reconciler was reading `totalTokens` and dumping it into
`tokens_in`, leaving `tokens_out`, `cache_read`, `cache_write` as
None. This made the Cost by Model sub-tab's Detail table show
`cache_read = 0` for all sessions even though real calls were
hitting the cache (e.g. M3 had ~1.6B cache_read tokens).

Fix: Read per-bucket fields from the OpenClaw session:
  - inputTokens  -> tokens_in
  - outputTokens -> tokens_out
  - cacheRead    -> cache_read
  - cacheWrite   -> cache_write

This test imports the actual reconciler module and calls
_process_sessions with a mock session list, then asserts the
emitted event record has the right per-bucket values.
"""
import sys
from pathlib import Path

CODE_DIR = Path(__file__).resolve().parent.parent / "code"
sys.path.insert(0, str(CODE_DIR))
from reconcile_openclaw_usage import _process_sessions  # noqa: E402


def test_per_bucket_values_used():
    """When the session has the new schema, the per-bucket values flow through."""
    sessions = [{
        "sessionId": "session-with-cache",
        "model": "MiniMax-M3",
        "kind": "group",
        "key": "agent:main:discord:channel:1",
        "inputTokens": 112998,
        "outputTokens": 18801,
        "cacheRead": 94826,
        "cacheWrite": 1234,
        "totalTokens": 226625,  # input + output + cacheRead + cacheWrite
    }]
    state = {}
    # dry_run=True so we don't actually write the events log
    seen, new, skipped = _process_sessions(sessions, state, dry_run=True)
    assert seen == 1
    assert new == 1
    assert skipped == 0
    # Inspect the stored rec (in `state["seen"]`)
    assert "session-with-cache" in state["seen"]
    # We need to capture the rec — but _process_sessions doesn't
    # return it. So we use a side channel: monkey-patch the
    # `_append_event` symbol to capture what gets written.
    captured = []
    import reconcile_openclaw_usage as r
    orig_append = r._append_event
    r._append_event = lambda rec: captured.append(rec)
    try:
        # Reset state and re-run to actually emit
        state2 = {}
        _process_sessions(sessions, state2, dry_run=False)
    finally:
        r._append_event = orig_append
    assert len(captured) == 1, f"expected 1 event, got {len(captured)}"
    rec = captured[0]
    assert rec["tokens_in"] == 112998, f"tokens_in: {rec['tokens_in']}"
    assert rec["tokens_out"] == 18801, f"tokens_out: {rec['tokens_out']}"
    assert rec["cache_read"] == 94826, f"cache_read: {rec['cache_read']}"
    assert rec["cache_write"] == 1234, f"cache_write: {rec['cache_write']}"
    print("OK per-bucket values flow through (tokens_in, tokens_out, cache_read, cache_write)")


def test_fallback_to_totalTokens():
    """Old schema (only totalTokens) still records something."""
    sessions = [{
        "sessionId": "old-schema-session",
        "model": "MiniMax-M3",
        "kind": "group",
        "key": "agent:main:discord:channel:1",
        "totalTokens": 50000,
        # No inputTokens / outputTokens / cacheRead / cacheWrite
    }]
    captured = []
    import reconcile_openclaw_usage as r
    orig_append = r._append_event
    r._append_event = lambda rec: captured.append(rec)
    try:
        _process_sessions(sessions, {}, dry_run=False)
    finally:
        r._append_event = orig_append
    assert len(captured) == 1
    rec = captured[0]
    # Old behavior: totalTokens -> tokens_in
    assert rec["tokens_in"] == 50000
    assert rec["cache_read"] == 0
    assert rec["cache_write"] == 0
    print("OK old schema falls back to totalTokens -> tokens_in (backward compatible)")


def test_zero_cache_no_crash():
    """New schema with 0 cache tokens doesn't crash and writes zeros."""
    sessions = [{
        "sessionId": "no-cache-session",
        "model": "grok-4.3",
        "kind": "group",
        "key": "agent:main:discord:channel:1",
        "inputTokens": 1000,
        "outputTokens": 500,
        "cacheRead": 0,
        "cacheWrite": 0,
        "totalTokens": 1500,
    }]
    captured = []
    import reconcile_openclaw_usage as r
    orig_append = r._append_event
    r._append_event = lambda rec: captured.append(rec)
    try:
        _process_sessions(sessions, {}, dry_run=False)
    finally:
        r._append_event = orig_append
    rec = captured[0]
    assert rec["tokens_in"] == 1000
    assert rec["tokens_out"] == 500
    assert rec["cache_read"] == 0
    assert rec["cache_write"] == 0
    print("OK zero-cache sessions record cleanly (no crash, no None)")


if __name__ == "__main__":
    test_per_bucket_values_used()
    test_fallback_to_totalTokens()
    test_zero_cache_no_crash()
    print()
    print("ALL CHECKS PASSED ✅")
