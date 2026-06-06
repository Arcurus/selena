#!/usr/bin/env python3
"""
backfill_todo_projects.py
=========================

One-shot (and safe to re-run) heuristic to fill in the `project` field for
existing todos that don't have it set. (Added 2026-06-03 per Arcurus when
the project + agent_owner fields were added to the todo schema.)

Rules (keyword -> project, in priority order):
  selena-project-2, lunar, lunar-project -> selena-project-lunar
  selena-project (main), heartbeat, web ui, knowledge, cron, agent -> selena-project
  moderation, openlife -> openlife
  open-world, world, world_data, entity -> open-world-selena
  Everything else stays "unassigned".

Usage:
  python3 selena-project/scripts/backfill_todo_projects.py
  python3 selena-project/scripts/backfill_todo_projects.py --dry-run
"""

import argparse
import json
import os

TODO_PATH = os.path.expanduser("~/openclaw/workspace/selena-project/data/todos.json")

RULES = [
    ("selena-project-2", "selena-project-lunar"),
    ("lunar", "selena-project-lunar"),
    ("lunar-project", "selena-project-lunar"),
    ("selena-project (main)", "selena-project"),
    ("heartbeat", "selena-project"),
    ("web ui", "selena-project"),
    ("knowledge", "selena-project"),
    ("agent_owner", "selena-project"),
    ("moderation", "openlife"),
    ("openlife", "openlife"),
    ("open-world", "open-world-selena"),
    ("world_data", "open-world-selena"),
    ("entity", "open-world-selena"),
    ("world", "open-world-selena"),
]


def infer(short_desc: str, long_desc: str) -> str | None:
    text = ((short_desc or "") + " " + (long_desc or "")).lower()
    for kw, proj in RULES:
        if kw in text:
            return proj
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="Don't write changes back")
    args = ap.parse_args()

    if not os.path.exists(TODO_PATH):
        print(f"error: {TODO_PATH} not found")
        return 1
    with open(TODO_PATH) as f:
        todos = json.load(f)
    print(f"loaded {len(todos)} todos from {TODO_PATH}")

    updated = 0
    for t in todos:
        if t.get("project") in (None, "", "unassigned"):
            proj = infer(t.get("short_desc", ""), t.get("long_desc", ""))
            if proj:
                t["project"] = proj
                updated += 1
    print(f"would set project on {updated} todos")

    if updated == 0 or args.dry_run:
        return 0

    with open(TODO_PATH, "w") as f:
        json.dump(todos, f, indent=2, ensure_ascii=False)
    print(f"wrote {TODO_PATH}")

    from collections import Counter
    c = Counter(t.get("project") or "unassigned" for t in todos)
    print("project breakdown:")
    for proj, n in c.most_common():
        print(f"  {proj}: {n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
