#!/usr/bin/env python3
"""
One-shot backfill: rewrite data/llm_usage_events.jsonl so events
from the reconciler have the correct per-bucket token counts
(tokens_in / tokens_out / cache_read / cache_write) instead of
all-of-totalTokens-as-tokens_in-and-rest-None.

Bug context: until 2026-06-12, the reconciler
(code/reconcile_openclaw_usage.py) was reading only `totalTokens`
from the openclaw session dump and dumping it into `tokens_in`,
leaving the other buckets as None/0. That made the per-model Cost
by Model sub-tab show cache_read=0 for all sessions even though
real calls were hitting the cache (M3 alone had ~1.6B cache_read
tokens). The reconciler is now fixed (reads inputTokens /
outputTokens / cacheRead / cacheWrite per session), but the
historical events are still broken.

This script rewrites the events log in place, looking up the
per-bucket values from data/openclaw_usage.jsonl (which has them
correctly) by sessionId. It's idempotent — re-running does
nothing to events that are already correct.

Safety:
  - Backs up the original file to
    data/llm_usage_events.jsonl.bak-<UTC-timestamp> first.
  - Writes the new file to data/llm_usage_events.jsonl.tmp,
    then atomically renames.
  - Skips events that don't have a sessionId (direct in-process
    calls, not from the reconciler).
  - Logs a summary to stdout.

Usage:
  python3 scripts/backfill_cache_tokens.py [--dry-run]
"""
import argparse
import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EVENTS = ROOT / "data" / "llm_usage_events.jsonl"
OPENCLAW_USAGE = ROOT / "data" / "openclaw_usage.jsonl"


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true",
                   help="Show what would change but don't write anything")
    args = p.parse_args()

    if not EVENTS.exists():
        print(f"FAIL: {EVENTS} does not exist")
        return 1
    if not OPENCLAW_USAGE.exists():
        print(f"FAIL: {OPENCLAW_USAGE} does not exist (need it as the per-bucket source)")
        return 1

    # Load openclaw_usage into a dict by sessionId.  Keep the most
    # recent row per sessionId (in case there are duplicates).
    oc_by_sid = {}
    with OPENCLAW_USAGE.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            sid = rec.get("sessionId")
            if not sid:
                continue
            ts = rec.get("ts") or rec.get("updatedAt") or ""
            existing = oc_by_sid.get(sid)
            existing_ts = (existing.get("ts") or existing.get("updatedAt") or "") if existing else ""
            if existing is None or ts > existing_ts:
                oc_by_sid[sid] = rec
    print(f"Loaded {len(oc_by_sid):,} openclaw_usage sessions")

    # Walk the events log, fixing any event from the reconciler
    # whose per-bucket fields don't match the openclaw source.
    fixed = 0
    unchanged = 0
    skipped = 0
    no_match = 0
    new_lines = []
    with EVENTS.open() as f:
        for line in f:
            line = line.rstrip("\n")
            if not line:
                new_lines.append(line)
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                new_lines.append(line)
                skipped += 1
                continue
            # Only touch events from the reconciler; in-process
            # record() calls have accurate fields already.
            if rec.get("source") != "openclaw-usage-reconciler":
                new_lines.append(line)
                unchanged += 1
                continue
            sid = rec.get("sessionId")
            if not sid:
                new_lines.append(line)
                skipped += 1
                continue
            oc = oc_by_sid.get(sid)
            if oc is None:
                # No openclaw row for this sessionId — leave as-is
                new_lines.append(line)
                no_match += 1
                continue
            new_in = oc.get("tokensIn") or 0
            new_out = oc.get("tokensOut") or 0
            new_cr = oc.get("cacheRead") or 0
            new_cw = oc.get("cacheWrite") or 0
            old_in = rec.get("tokens_in") or 0
            old_out = rec.get("tokens_out") or 0
            old_cr = rec.get("cache_read") or 0
            old_cw = rec.get("cache_write") or 0
            if (old_in == new_in and old_out == new_out
                    and old_cr == new_cr and old_cw == new_cw):
                new_lines.append(line)
                unchanged += 1
                continue
            rec["tokens_in"] = new_in
            rec["tokens_out"] = new_out
            rec["cache_read"] = new_cr
            rec["cache_write"] = new_cw
            new_lines.append(json.dumps(rec))
            fixed += 1

    print(f"Events: {len(new_lines) - skipped:,} total, "
          f"fixed: {fixed:,}, unchanged: {unchanged:,}, "
          f"no openclaw match: {no_match:,}, malformed: {skipped:,}")

    if args.dry_run:
        print("DRY-RUN: no changes written")
        return 0

    if fixed == 0:
        print("Nothing to do (all events already correct)")
        return 0

    # Backup the original
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = EVENTS.with_suffix(f".jsonl.bak-{ts}")
    shutil.copy2(EVENTS, backup)
    print(f"Backed up to {backup}")

    # Atomic write: write to .tmp, then rename
    tmp = EVENTS.with_suffix(".jsonl.tmp")
    with tmp.open("w") as f:
        f.write("\n".join(new_lines) + "\n")
    os.replace(tmp, EVENTS)
    print(f"Rewrote {EVENTS} ({fixed:,} events fixed)")

    # Sanity check: re-aggregate and report the new per-model
    # cache_read totals so the user can see the effect
    from collections import defaultdict
    by_model = defaultdict(lambda: [0, 0, 0, 0])  # in, out, cr, cw
    with EVENTS.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            m = rec.get("model", "?")
            by_model[m][0] += rec.get("tokens_in") or 0
            by_model[m][1] += rec.get("tokens_out") or 0
            by_model[m][2] += rec.get("cache_read") or 0
            by_model[m][3] += rec.get("cache_write") or 0
    print()
    print("Per-model totals after backfill (all-time):")
    for m, (tin, tout, cr, cw) in sorted(by_model.items()):
        if cr > 0 or cw > 0:
            print(f"  {m:<35} in={tin:>15,} out={tout:>12,} "
                  f"cr={cr:>15,} cw={cw:>12,}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
