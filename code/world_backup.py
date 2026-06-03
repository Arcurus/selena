#!/usr/bin/env python3
"""
Daily World Backup for Open World
==================================

Per Arcurus (2026-06-03, #openworld):
  - Each day, take a copy of open-world-selena's save.owbl into
    world_data/backups/save-daily-YYYYMMDD.owbl
  - Auto-prune copies older than 30 days
  - If today's backup is suddenly < 50% the size of the previous
    daily backup, post a warning to #openworld

The size check protects against silent corruption (truncated save,
failed entity restore, etc.) which would otherwise be invisible until
we tried to load the world.

CLI:
    python3 world_backup.py status
    python3 world_backup.py run          # do the daily cycle
    python3 world_backup.py prune        # remove >30-day-old backups
    python3 world_backup.py list         # show all daily backups

API: see api_server.py under /api/world/backup/*
"""

import argparse
import json
import os
import re
import shutil
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

OW_ROOT = "/home/openclaw/openclaw/workspace/open-world-selena"
SAVE_PATH = os.path.join(OW_ROOT, "world_data", "save.owbl")
BACKUP_DIR = os.path.join(OW_ROOT, "world_data", "backups")

# Discord channel for warnings (Selena Astra → #openworld).
# Per MEMORY.md and the Discord channel metadata.
DEFAULT_WARN_CHANNEL_ID = "1511711727711031367"  # #openworld

# Thresholds
RETENTION_DAYS = 30
SIZE_DROP_WARN_RATIO = 0.5  # warn if new < 50% of previous

# Backup filename pattern
DAILY_NAME_RE = re.compile(r"^save-daily-(\d{8})\.owbl$")


# ---------------------------------------------------------------------------
# Bot token (mirrors cost_tracker.py)
# ---------------------------------------------------------------------------

def _get_bot_token() -> Optional[str]:
    """Read Discord bot token from OpenClaw config."""
    cfg_path = os.path.expanduser("~/.openclaw/openclaw.json")
    try:
        with open(cfg_path) as f:
            cfg = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    for path in (("discord", "token"),
                 ("channels", "discord", "token"),
                 ("plugins", "discord", "token")):
        node: Any = cfg
        ok = True
        for k in path:
            if not isinstance(node, dict) or k not in node:
                ok = False
                break
            node = node[k]
        if ok and isinstance(node, str):
            return node
    return None


def _discord_post(channel_id: str, text: str) -> Dict[str, Any]:
    """POST a message to a Discord channel via the bot token."""
    token = _get_bot_token()
    if not token:
        return {"error": "no_bot_token"}
    url = f"https://discord.com/api/v10/channels/{channel_id}/messages"
    body = json.dumps({"content": text[:2000]}).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={
            "Authorization": f"Bot {token}",
            "Content-Type": "application/json",
            "User-Agent": "selena-world-backup/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        return {"error": f"http_{e.code}", "body": e.read().decode("utf-8", errors="ignore")[:300]}
    except urllib.error.URLError as e:
        return {"error": f"url_error: {e.reason}"}


# ---------------------------------------------------------------------------
# Activity log (mirrors other modules)
# ---------------------------------------------------------------------------

ACTIVITY_LOG = "/home/openclaw/openclaw/workspace/selena-project/data/activity_log"


def _log_activity(kind: str, message: str) -> None:
    """Append an entry to the activity log; best-effort, never raises."""
    try:
        os.makedirs(os.path.dirname(ACTIVITY_LOG), exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        with open(ACTIVITY_LOG, "a") as f:
            f.write(f"[{ts}] [{kind}] {message}\n")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Backup operations
# ---------------------------------------------------------------------------

def _today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d")


def _today_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def list_daily_backups() -> List[Dict[str, Any]]:
    """List all daily backups, newest first.

    Each entry: {"date": "YYYYMMDD", "date_iso": "YYYY-MM-DD", "path": ..., "size_bytes": ...}
    """
    out: List[Dict[str, Any]] = []
    if not os.path.isdir(BACKUP_DIR):
        return out
    for name in os.listdir(BACKUP_DIR):
        m = DAILY_NAME_RE.match(name)
        if not m:
            continue
        date = m.group(1)
        path = os.path.join(BACKUP_DIR, name)
        try:
            st = os.stat(path)
            out.append({
                "date": date,
                "date_iso": f"{date[:4]}-{date[4:6]}-{date[6:8]}",
                "path": path,
                "size_bytes": st.st_size,
                "mtime": int(st.st_mtime),
            })
        except FileNotFoundError:
            continue
    out.sort(key=lambda e: e["date"], reverse=True)
    return out


def prune_old_backups(retention_days: int = RETENTION_DAYS,
                      now: Optional[datetime] = None) -> List[str]:
    """Remove daily backups older than retention_days. Returns paths removed."""
    if now is None:
        now = datetime.now(timezone.utc)
    cutoff_date = (now - timedelta(days=retention_days)).strftime("%Y%m%d")
    removed: List[str] = []
    for entry in list_daily_backups():
        if entry["date"] < cutoff_date:
            try:
                os.remove(entry["path"])
                removed.append(entry["path"])
                _log_activity("BACKUP_PRUNE",
                              f"Removed {os.path.basename(entry['path'])} "
                              f"({entry['date_iso']}, {entry['size_bytes']}B) "
                              f"older than {retention_days} days")
            except OSError as e:
                _log_activity("BACKUP_PRUNE_ERROR",
                              f"Failed to remove {entry['path']}: {e}")
    return removed


def _file_size(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def take_daily_backup(now: Optional[datetime] = None,
                      warn_channel_id: Optional[str] = None
                      ) -> Dict[str, Any]:
    """Take today's daily backup, prune old ones, check for size drop.

    Returns a status dict suitable for the CLI / API.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    if warn_channel_id is None:
        warn_channel_id = os.environ.get("WORLD_BACKUP_WARN_CHANNEL_ID",
                                         DEFAULT_WARN_CHANNEL_ID)

    result: Dict[str, Any] = {
        "ok": False,
        "today": _today_iso(),
        "saved_to": None,
        "previous": None,
        "size_bytes": 0,
        "previous_size_bytes": 0,
        "ratio": None,
        "warning_posted": False,
        "warning_text": None,
        "pruned": [],
    }

    if not os.path.exists(SAVE_PATH):
        result["error"] = "save.owbl not found"
        _log_activity("BACKUP_SKIP", result["error"])
        return result

    # 1) Find the previous daily backup (the most recent one strictly
    #    before today).  If today's already exists, treat today as the
    #    "previous" so we still run the size check vs the same file.
    today_str = now.strftime("%Y%m%d")
    today_path = os.path.join(BACKUP_DIR, f"save-daily-{today_str}.owbl")
    daily = list_daily_backups()
    previous = None
    for entry in daily:
        if entry["date"] == today_str:
            continue
        previous = entry
        break

    # 2) Copy the current save to today's slot (overwrite if exists so
    #    a same-day re-run reflects the latest state).
    os.makedirs(BACKUP_DIR, exist_ok=True)
    try:
        shutil.copy2(SAVE_PATH, today_path)
    except OSError as e:
        result["error"] = f"copy failed: {e}"
        _log_activity("BACKUP_COPY_ERROR", result["error"])
        return result

    new_size = _file_size(today_path)
    result["saved_to"] = today_path
    result["size_bytes"] = new_size

    if previous is not None:
        prev_size = previous["size_bytes"]
        result["previous"] = previous["path"]
        result["previous_size_bytes"] = prev_size
        if prev_size > 0:
            result["ratio"] = round(new_size / prev_size, 3)
            if new_size < prev_size * SIZE_DROP_WARN_RATIO:
                # The new backup suddenly lost a lot of weight.  Post a
                # warning to Discord so a human can investigate.
                warn_text = (
                    f"⚠️ **World backup size dropped**\n"
                    f"• Today: `{today_str}.owbl` — **{new_size:,} B**\n"
                    f"• Previous: `{previous['date_iso']}.owbl` — "
                    f"**{prev_size:,} B**\n"
                    f"• Ratio: **{result['ratio']:.0%}** "
                    f"(threshold: {SIZE_DROP_WARN_RATIO:.0%})\n"
                    f"Possible causes: world was wiped, entities were "
                    f"deleted, or the save file is corrupt.  "
                    f"Path: `world_data/backups/{os.path.basename(today_path)}`"
                )
                if os.environ.get("WORLD_BACKUP_DRY_RUN") == "1":
                    result["warning_text"] = warn_text
                else:
                    post = _discord_post(warn_channel_id, warn_text)
                    if "error" not in post:
                        result["warning_posted"] = True
                        _log_activity("BACKUP_WARN_POSTED",
                                      f"Posted size-drop warning to channel "
                                      f"{warn_channel_id} "
                                      f"({new_size}B vs prev {prev_size}B)")
                    else:
                        result["warning_text"] = warn_text
                        result["warning_post_error"] = post.get("error")
                        _log_activity("BACKUP_WARN_FAILED",
                                      f"Could not post warning: {post.get('error')}")

    # 3) Prune old backups
    result["pruned"] = prune_old_backups(now=now)

    result["ok"] = True
    _log_activity("BACKUP_OK",
                  f"daily backup {today_str}.owbl = {new_size}B, "
                  f"pruned {len(result['pruned'])} old file(s)")
    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(prog="world_backup",
                                     description="Daily backup + retention for open-world-selena")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="List existing daily backups (newest first)")
    sub.add_parser("prune", help="Remove daily backups older than 30 days")
    sub.add_parser("status", help="Show summary: how many backups, oldest, newest, total bytes")

    p_run = sub.add_parser("run", help="Take today's daily backup + prune + size check")
    p_run.add_argument("--warn-channel", help="Discord channel ID for size-drop warnings")
    p_run.add_argument("--dry-run", action="store_true",
                       help="Compute everything but don't write or post")

    args = parser.parse_args(argv[1:])

    if getattr(args, "dry_run", False):
        os.environ["WORLD_BACKUP_DRY_RUN"] = "1"

    if args.cmd == "list":
        backups = list_daily_backups()
        if not backups:
            print("(no daily backups yet)")
            return 0
        print(f"{'date':10s}  {'size_bytes':>10s}  path")
        for e in backups:
            print(f"{e['date_iso']:10s}  {e['size_bytes']:>10d}  {e['path']}")
        return 0

    if args.cmd == "prune":
        removed = prune_old_backups()
        print(f"Pruned {len(removed)} backup(s) older than {RETENTION_DAYS} days:")
        for p in removed:
            print(f"  - {p}")
        return 0

    if args.cmd == "status":
        backups = list_daily_backups()
        if not backups:
            print(json.dumps({"count": 0}, indent=2))
            return 0
        total = sum(b["size_bytes"] for b in backups)
        print(json.dumps({
            "count": len(backups),
            "newest": backups[0]["date_iso"],
            "oldest": backups[-1]["date_iso"],
            "total_bytes": total,
            "retention_days": RETENTION_DAYS,
            "warn_ratio": SIZE_DROP_WARN_RATIO,
        }, indent=2))
        return 0

    if args.cmd == "run":
        warn_channel = None
        if getattr(args, "warn_channel", None):
            warn_channel = args.warn_channel
        result = take_daily_backup(warn_channel_id=warn_channel)
        print(json.dumps(result, indent=2))
        # Exit non-zero if something went wrong so cron can see.
        return 0 if result.get("ok") else 1

    return 2  # unknown subcommand


if __name__ == "__main__":
    sys.exit(main(sys.argv))
