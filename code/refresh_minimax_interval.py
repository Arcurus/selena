#!/usr/bin/env python3
"""
Refresh the `minimax_interval` block of data/llm_usage_snapshot.json from
`mmx quota`, the same way budget_gate.py does it.

Per Arcurus 2026-06-10 in #cost-tracker: the snapshot's
`providers.minimax.quota` field has been stale since 2026-06-08
(because the direct `/v1/token_plan/remains` API call broke), and
the snapshot's `minimax_interval` field hasn't been refreshed since
2026-06-10T15:39Z (because `sync_quotas` in llm_call_tracker.py is
a no-op). This script is the new cron-driven refresher for the
`minimax_interval` block; budget_gate.py already covers the
budget_gate.json file separately, but the snapshot is read by the
cost report, the optimisation manager, and many other places that
need a single source of truth.

Usage:
    python3 code/refresh_minimax_interval.py        # Update the snapshot
    python3 code/refresh_minimax_interval.py --json # Print JSON only, no file write

Atomic write: writes to .tmp then renames. Keeps a .bak for forensics.

Tolerant: on any error (mmx missing, timeout, parse failure), exits 1
without modifying the snapshot — the cron is allowed to silently
fail, and the next successful run will catch up. Errors are logged
to stderr.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

SELENA_ROOT = os.path.expanduser("~/openclaw/workspace/selena-project")
SNAPSHOT_PATH = os.path.join(SELENA_ROOT, "data", "llm_usage_snapshot.json")

_MMX_CANDIDATES = [
    os.environ.get("MMX_BIN"),
    os.path.expanduser("~/.npm-global/bin/mmx"),
    os.path.expanduser("~/openclaw/.npm-global/bin/mmx"),
    "/home/openclaw/.npm-global/bin/mmx",
    shutil.which("mmx"),
]
MMX_BIN = next((c for c in _MMX_CANDIDATES if c and (c == "mmx" or os.path.isfile(c))), "mmx")
MMX_TIMEOUT_S = 15


def _log(msg: str) -> None:
    line = f"[{datetime.now(timezone.utc).isoformat()}] {msg}"
    print(line, file=sys.stderr)


def pull_mmx_quota() -> Dict[str, Any]:
    """Run `mmx quota` and return the parsed JSON. Never raises.

    Returns a dict shaped like the budget_gate parser's output:
      { "ok": True, "models": {<name>: {...}}, "soonest_reset_s": <int>, ... }
    On failure: { "ok": False, "error": "...", "models": {} }
    """
    out: Dict[str, Any] = {
        "ok": False,
        "source": "mmx-cli",
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    try:
        proc = subprocess.run(
            [MMX_BIN, "quota"],
            capture_output=True, text=True, timeout=MMX_TIMEOUT_S,
        )
    except FileNotFoundError:
        out["error"] = f"mmx binary not found at {MMX_BIN}"
        return out
    except subprocess.TimeoutExpired:
        out["error"] = f"mmx quota timed out after {MMX_TIMEOUT_S}s"
        return out
    except Exception as e:  # noqa: BLE001
        out["error"] = f"mmx quota error: {type(e).__name__}: {e}"
        return out
    if proc.returncode != 0:
        out["error"] = f"mmx exited {proc.returncode}: {(proc.stderr or '').strip()[:200]}"
        return out
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        out["error"] = f"mmx returned non-JSON: {e}"
        out["raw"] = (proc.stdout or "")[:400]
        return out
    out["ok"] = True
    out["data"] = data
    return out


def _parse(raw_data: Dict[str, Any]) -> Tuple[Dict[str, Dict[str, Any]], Optional[int], int]:
    """Parse `mmx quota` JSON into a snapshot-shaped `minimax_interval.models` dict.

    Returns (models, soonest_reset_s, grand_total_used).
    """
    models: Dict[str, Dict[str, Any]] = {}
    soonest_reset_s: Optional[int] = None
    grand_total_used = 0
    for entry in (raw_data.get("model_remains") or []):
        if not isinstance(entry, dict):
            continue
        name = entry.get("model_name") or "?"
        rem = entry.get("current_interval_remaining_percent")
        weekly_rem = entry.get("current_weekly_remaining_percent")
        used = entry.get("current_interval_usage_count") or 0
        total = entry.get("current_interval_total_count") or 0
        status = entry.get("current_interval_status")
        rt = entry.get("remains_time")
        rt_s: Optional[int] = None
        if isinstance(rt, (int, float)):
            rt_s = int(rt) // 1000
        models[name] = {
            "remaining_percent": rem,
            "weekly_remaining_percent": weekly_rem,
            "used": used,
            "total": total,
            "status": status,
            "resets_in_s": rt_s,
        }
        grand_total_used += int(used or 0)
        if rt_s is not None and (soonest_reset_s is None or rt_s > soonest_reset_s):
            soonest_reset_s = rt_s
    return models, soonest_reset_s, grand_total_used


def build_minimax_interval_block(quota: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Build the new `minimax_interval` block from a `pull_mmx_quota()` result.

    Returns None if `quota.ok` is False, so the caller can decide to skip
    the write rather than overwrite a good block with an error stub.
    """
    if not quota.get("ok"):
        return None
    raw = quota.get("data") or {}
    models, soonest_reset_s, grand_total_used = _parse(raw)
    return {
        "ok": True,
        "error": None,
        "fetched_at": quota.get("ts") or datetime.now(timezone.utc).isoformat(),
        "models": models,
        "soonest_reset_s": soonest_reset_s or 0,
        "grand_total_used": grand_total_used,
    }


def _atomic_write_json(path: str, payload: Dict[str, Any]) -> None:
    """Write JSON atomically: .tmp + os.replace. Keeps a .bak of the prior file."""
    if os.path.exists(path):
        try:
            shutil.copy2(path, path + ".bak")
        except OSError:
            pass
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    os.replace(tmp, path)


def refresh_snapshot(quota: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Refresh `minimax_interval` in the snapshot. Returns the new block.

    Args:
        quota: pre-pulled `mmx quota` result; if None, we'll pull it here.
    """
    if quota is None:
        quota = pull_mmx_quota()
    if not quota.get("ok"):
        _log(f"skip: mmx quota failed: {quota.get('error')}")
        return {"ok": False, "error": quota.get("error")}
    new_block = build_minimax_interval_block(quota)
    if new_block is None:
        return {"ok": False, "error": "build_minimax_interval_block returned None"}
    # Load existing snapshot (or start from empty)
    snap: Dict[str, Any] = {}
    if os.path.exists(SNAPSHOT_PATH):
        try:
            with open(SNAPSHOT_PATH, "r", encoding="utf-8") as f:
                snap = json.load(f) or {}
        except (OSError, json.JSONDecodeError) as e:
            _log(f"snapshot load error (continuing with fresh): {e}")
            snap = {}
    snap["minimax_interval"] = new_block
    # Bump generated_at to the time of the refresh, so any downstream
    # reader can tell at a glance how old the snapshot is.
    snap["generated_at"] = datetime.now(timezone.utc).isoformat()
    _atomic_write_json(SNAPSHOT_PATH, snap)
    return new_block


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--json", action="store_true", help="Print result JSON to stdout, do not modify the snapshot")
    args = p.parse_args()
    quota = pull_mmx_quota()
    if args.json:
        if not quota.get("ok"):
            print(json.dumps({"ok": False, "error": quota.get("error")}, indent=2))
            return 1
        block = build_minimax_interval_block(quota) or {}
        print(json.dumps(block, indent=2))
        return 0
    block = refresh_snapshot(quota)
    if not block.get("ok"):
        return 1
    print(json.dumps({
        "ok": True,
        "fetched_at": block.get("fetched_at"),
        "models": list((block.get("models") or {}).keys()),
        "general_remaining_pct": (block.get("models") or {}).get("general", {}).get("remaining_percent"),
    }, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
