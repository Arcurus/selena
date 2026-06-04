#!/usr/bin/env python3
"""
backfill_completed_at.py
========================
One-shot backfill for the new `completed_at` field on existing todos.

Rule (per Arcurus 2026-06-05):
- For any todo with status in (completed, done) AND completed_at is missing/None,
  set completed_at = updated_at (best available proxy for when it reached that
  terminal state). If updated_at is also missing, fall back to created_at.
- For any other status, leave completed_at alone (typically already None; the
  new auto-rule will keep it None for non-terminal states going forward).

Behaviour:
- Dry-run by default (prints a summary, writes nothing).
- Pass --apply to actually write back to the JSON files (creates the usual
  timestamped backup first via TodoManager's existing _backup_todos path —
  but here we use a separate backup so this script can run without going
  through TodoManager.save() to keep the audit trail distinct).
- Logs every change to data/backfill_completed_at_log.jsonl.

Usage:
  python3 code/backfill_completed_at.py            # dry-run, prints summary
  python3 code/backfill_completed_at.py --apply    # actually write + log
  python3 code/backfill_completed_at.py --apply --files todos.json
                                                    # restrict to one file
"""

import argparse
import json
import os
import sys
from datetime import datetime

AGENT_ROOT = os.path.expanduser("~/openclaw/workspace/selena-project")
DATA_DIR = os.path.join(AGENT_ROOT, "data")
BACKUP_DIR = os.path.join(DATA_DIR, "backups")
LOG_PATH = os.path.join(DATA_DIR, "backfill_completed_at_log.jsonl")

# Terminal states that should carry a completed_at value.
TERMINAL_STATES = ("completed", "done")


def _now() -> str:
    return datetime.now().isoformat()


def _load(path):
    if not os.path.exists(path):
        return []
    with open(path, 'r') as f:
        try:
            data = json.load(f)
        except json.JSONDecodeError:
            return []
    if isinstance(data, dict) and "todos" in data:
        return data["todos"]
    if isinstance(data, list):
        return data
    return []


def _save(path, todos):
    with open(path, 'w') as f:
        json.dump(todos, f, indent=2)


def _backup(path):
    if not os.path.exists(path):
        return None
    os.makedirs(BACKUP_DIR, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    base = os.path.basename(path)
    backup_name = f"todos_backup_{ts}_pre_completed_at_backfill_{base}"
    backup_path = os.path.join(BACKUP_DIR, backup_name)
    with open(path, 'r') as src, open(backup_path, 'w') as dst:
        dst.write(src.read())
    return backup_path


def backfill_file(path, apply_changes, log_handle):
    """Backfill completed_at on a single todos file. Returns (changed, scanned)."""
    todos = _load(path)
    changed = 0
    scanned = 0
    for t in todos:
        scanned += 1
        status = t.get("status")
        if status not in TERMINAL_STATES:
            continue
        # Skip if already filled
        if t.get("completed_at"):
            continue
        # Pick best proxy: updated_at > created_at
        proxy = t.get("updated_at") or t.get("created_at") or _now()
        old = t.get("completed_at")
        t["completed_at"] = proxy
        changed += 1
        record = {
            "ts": _now(),
            "file": os.path.basename(path),
            "id": t.get("id"),
            "short_desc": t.get("short_desc"),
            "status": status,
            "old_completed_at": old,
            "new_completed_at": proxy,
            "source": "updated_at" if t.get("updated_at") == proxy else
                      "created_at" if t.get("created_at") == proxy else "now",
        }
        log_handle.write(json.dumps(record) + "\n")
    if apply_changes and changed:
        backup = _backup(path)
        _save(path, todos)
        print(f"  backed up to: {backup}")
    return changed, scanned


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="Write changes (default: dry-run)")
    ap.add_argument("--files", nargs="*", default=None, help="Restrict to specific filenames (e.g. todos.json todos.env)")
    args = ap.parse_args()

    targets = ["todos.json", "todos.env"]
    if args.files:
        targets = [f for f in args.files]

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"=== backfill_completed_at.py [{mode}] ===")
    print(f"data dir: {DATA_DIR}")
    print(f"targets:  {targets}")
    print()

    log_handle = open(LOG_PATH, "a")
    grand_changed = 0
    grand_scanned = 0
    for name in targets:
        path = os.path.join(DATA_DIR, name)
        if not os.path.exists(path):
            print(f"[skip] {name} (not found)")
            continue
        changed, scanned = backfill_file(path, args.apply, log_handle)
        grand_changed += changed
        grand_scanned += scanned
        print(f"[{name}] scanned={scanned} would_change={changed}")
    log_handle.close()

    print()
    print(f"total scanned={grand_scanned}  total would_change={grand_changed}")
    if not args.apply and grand_changed:
        print()
        print("dry-run only — re-run with --apply to write changes.")
    if args.apply and grand_changed:
        print(f"changes logged to: {LOG_PATH}")


if __name__ == "__main__":
    main()
