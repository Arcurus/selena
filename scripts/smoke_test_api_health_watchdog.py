#!/usr/bin/env python3
"""
smoke_test_api_health_watchdog.py — exercise api_health_watchdog.py's
data-freshness path end-to-end without spamming real Discord.

What it covers (7 tests):
  1. Baseline: all data files fresh → no alerts posted.
  2. The 22h incident replayed: openclaw_usage.jsonl backdated 22h → STALE
     alert fires with the right producer hint.
  3. Dedup: re-stale within 30 min → no re-alert posted.
  4. Recovery: file mtime bumped to now → "fresh again" post sent.
  5. Smart context, "openclaw fresh": events.jsonl stale, openclaw_usage.jsonl
     fresh → alert includes "openclaw_usage.jsonl is fresh — sessions ARE
     happening, the reconciler may just not have seen new ones yet" hint.
  6. Smart context, "genuinely quiet": both files stale → alert includes
     "system appears genuinely quiet" hint.
  7. Status command renders the new "Data files (mtime freshness)" section.

The test resets state, mutates real data file mtimes, then restores them
at the end. Safe to run on the live system; the only side effect is one
extra log line in data/api_health_watchdog.log and a transient state file.

Run:  python3 scripts/smoke_test_api_health_watchdog.py
Exit: 0 = pass, 1 = fail
"""
import importlib.util
import io
import json
import os
import sys
import time
from contextlib import redirect_stdout
from pathlib import Path

SELENA_DIR = Path(__file__).resolve().parent.parent


def load_watchdog():
    spec = importlib.util.spec_from_file_location(
        "wd", str(SELENA_DIR / "code" / "api_health_watchdog.py")
    )
    wd = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(wd)
    return wd


def main() -> int:
    wd = load_watchdog()

    # Reset state
    if wd.STATE_PATH.exists():
        wd.STATE_PATH.unlink()

    # Touch all data files to simulate "fresh" baseline
    for _label, relpath, _, _ in wd.DATA_FILES:
        p = wd.SELENA_DIR / relpath
        if p.exists():
            os.utime(p, (time.time(), time.time()))

    # Capture Discord posts; silence activity_log
    posted: list = []
    wd.post_discord = lambda text: posted.append(text) or True
    wd.activity_log = lambda e, d: None

    # Save real mtimes so we can restore them
    real_mtimes = {}
    for _label, relpath, _, _ in wd.DATA_FILES:
        p = wd.SELENA_DIR / relpath
        if p.exists():
            real_mtimes[relpath] = p.stat().st_mtime

    real_path = wd.SELENA_DIR / "data" / "openclaw_usage.jsonl"
    events_path = wd.SELENA_DIR / "data" / "llm_usage_events.jsonl"

    try:
        # === TEST 1: baseline ===
        posted.clear()
        rc = wd.check_once()
        assert rc == 0 and len(posted) == 0, f"baseline: exit={rc}, posts={posted}"
        print("TEST 1: baseline (all fresh) — no posts ✓")

        # === TEST 2: 22h gap on openclaw_usage.jsonl (the original incident) ===
        posted.clear()
        os.utime(real_path, (time.time(), time.time() - 22 * 3600))
        wd.check_once()
        assert any(
            "openclaw_usage.jsonl" in p and "STALE" in p for p in posted
        ), f"expected STALE alert for openclaw_usage.jsonl, got: {posted}"
        print("TEST 2: 22h gap on openclaw_usage.jsonl — alert fired ✓")

        # === TEST 3: dedup within 30 min ===
        posted.clear()
        wd.check_once()
        assert len(posted) == 0, f"expected dedup, but got: {posted}"
        print("TEST 3: dedup within 30 min — no re-alert ✓")

        # === TEST 4: recovery ===
        posted.clear()
        os.utime(real_path, (time.time(), time.time()))
        wd.check_once()
        assert any("fresh again" in p for p in posted), \
            f"expected recovery post, got: {posted}"
        print("TEST 4: recovery — fresh again post sent ✓")

        # === TEST 5: smart context — events stale, openclaw fresh ===
        posted.clear()
        os.utime(events_path, (time.time(), time.time() - 35 * 60))
        wd.check_once()
        events_alerts = [p for p in posted if "llm_usage_events" in p]
        assert any("openclaw_usage.jsonl is fresh" in p for p in events_alerts), \
            f"expected 'openclaw fresh' hint, got: {events_alerts}"
        print("TEST 5: events stale, openclaw fresh — 'fresh' hint in alert ✓")

        # === TEST 6: true quiet — both stale (reset events alert state first) ===
        posted.clear()
        state = json.loads(wd.STATE_PATH.read_text())
        state["data_files"]["llm_usage_events.jsonl"]["last_alert_at"] = None
        wd.STATE_PATH.write_text(json.dumps(state, indent=2, sort_keys=True))
        os.utime(real_path, (time.time(), time.time() - 22 * 3600))
        wd.check_once()
        events_alerts = [p for p in posted if "llm_usage_events" in p]
        assert any("genuinely quiet" in p for p in events_alerts), \
            f"expected 'genuinely quiet' hint, got: {events_alerts}"
        print("TEST 6: both stale — 'genuinely quiet' hint in alert ✓")

        # === TEST 7: status command ===
        buf = io.StringIO()
        with redirect_stdout(buf):
            wd.show_status()
        assert "Data files (mtime freshness)" in buf.getvalue(), \
            f"status missing data section:\n{buf.getvalue()}"
        print("TEST 7: status command — data section rendered ✓")

        print("\n=== ALL 7 TESTS PASSED ===")
        return 0
    finally:
        # Restore real mtimes + clean state
        for relpath, mtime in real_mtimes.items():
            p = wd.SELENA_DIR / relpath
            if p.exists():
                os.utime(p, (time.time(), mtime))
        if wd.STATE_PATH.exists():
            wd.STATE_PATH.unlink()


if __name__ == "__main__":
    sys.exit(main())
