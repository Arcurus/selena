#!/usr/bin/env python3
"""
discord_lookup.py — Selena Project

Scans OpenLife Reborn channels for new messages from new users and (with
debounce) wakes the moderation cron via `openclaw cron run <jobId>`.

Run modes:
  - Cron-driven: openclaw cron entry at the configured interval (default */5)
  - Manual:      python3 discord_lookup.py scan --manual   (for the "Run Now" button)
  - Dry-run:     python3 discord_lookup.py scan --dry-run  (log only, no wake)

CLI subcommands:
  scan                              Run a scan cycle
      --manual                      Manual mode (records manual trigger in log)
      --dry-run                     Don't actually wake the cron, just log the decision
  status                            Show last scan, last wake, recent decisions
  settings get                      Show current settings
  settings set <key> <value>        Set a settings key
  triggers [--limit N]              Show recent trigger events
  users [--limit N] [--sort ...]    Show cached users
  users update-summary --id UID --summary TEXT
                                    Update the LLM-generated community-standing
                                    summary for a user (called by the moderation cron)
  trigger-cron [--job-id ID]        Force-wake the moderation cron (bypasses debounce)
  archive [--limit N]               Read moderation_actions_archive.jsonl
                                    (action_filter: --action ban|timeout|both, default both)
  policies                          Read moderation_policies.md
  docs                              Read docs/moderation.md
  pending list                      List pending (dry-run / unexecuted) actions
  pending add                       Add a pending action (does NOT execute it)
      --target-user-id UID          (required) Discord user ID
      --target-username NAME        (optional) display name
      --action <ban_user|timeout_user>
                                    (required)
      --duration DUR                (required for timeout_user) e.g. 1h, 24h, 7d
      --reason TEXT                 (required) the reason / policy cite
      --source <manual|test|...>    (optional, default "manual")
  pending edit --id ID --reason TEXT
                                    Edit the reason of a pending action
  pending delete --id ID            Remove a pending action (does NOT execute)
  pending apply --id ID             Execute a single pending action
  pending apply --all               Execute ALL pending actions

State files (all under selena-project/data/):
  moderation_settings.json                     Settings (interval, channels, debounce, etc.)
  moderation_state/discord_lookup_state.json   Per-channel last_message_id, last_wake_at, counters
  moderation_state/users_cache.json            Per-user history (username, message_count, etc.)
  moderation_triggers.jsonl                    Append-only trigger log

Reuses from moderation_check.py:
  _api_headers, validate_bot_token, all_protected_users, GUILD_ID
"""

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

import requests

# Reuse the moderation_check.py infrastructure
sys.path.insert(0, str(Path(__file__).parent))
try:
    import moderation_check as mc
    _api_headers = mc._api_headers
    _validate_bot_token = mc.validate_bot_token
    _all_protected_users = mc.all_protected_users
    GUILD_ID = mc.GUILD_ID
except ImportError as e:
    print(f"ERROR: cannot import moderation_check: {e}", file=sys.stderr)
    sys.exit(2)

# -----------------------------------------------------------------------------
# Paths
# -----------------------------------------------------------------------------
HERE = Path(__file__).resolve().parent
SELENA_PROJECT = HERE.parent
DATA = SELENA_PROJECT / "data"
STATE_DIR = DATA / "moderation_state"
SETTINGS_PATH = DATA / "moderation_settings.json"
STATE_PATH = STATE_DIR / "discord_lookup_state.json"
USERS_CACHE_PATH = STATE_DIR / "users_cache.json"
TRIGGERS_LOG_PATH = DATA / "moderation_triggers.jsonl"
PENDING_ACTIONS_PATH = DATA / "pending_actions.json"
PENDING_URGENT_PATH = DATA / "pending_actions_urgent.json"

# Default moderation cron job id (per ~/.openclaw/cron/jobs.json)
DEFAULT_MODERATION_CRON_JOB_ID = "1b0f1a2b-5677-4e8e-9699-17c29e55014c"

DEFAULT_SETTINGS: dict[str, Any] = {
    "interval_minutes": 5,
    "channels": [
        # OpenLife Reborn channels (from existing moderation state)
        "985999745329790976",
        "985999783778988092",  # #pictures
        "1066137641948557442",  # #welcome
        "1474069653210009802",
        "985997281734041683",
        "986039632426848346",
        "985999865211392050",
        "985999655970160670",
        "987054244718870600",
        "1174021291657928784",
    ],
    "debounce_minutes": 5,
    "auto_trigger": True,
    "dry_run": False,
    "lookback_messages": 50,
    "new_user_max_history": 5,
    "new_user_max_account_age_days": 30,
    "moderation_cron_job_id": DEFAULT_MODERATION_CRON_JOB_ID,
    "discord_guild_id": GUILD_ID,
    "fetch_timeout_seconds": 15,
}


# -----------------------------------------------------------------------------
# Settings + state I/O
# -----------------------------------------------------------------------------
def _ensure_dirs():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    DATA.mkdir(parents=True, exist_ok=True)


def load_settings() -> dict[str, Any]:
    _ensure_dirs()
    if not SETTINGS_PATH.exists():
        save_settings(DEFAULT_SETTINGS)
        return dict(DEFAULT_SETTINGS)
    with open(SETTINGS_PATH, encoding="utf-8") as f:
        s = json.load(f)
    # Backfill any missing keys from defaults
    for k, v in DEFAULT_SETTINGS.items():
        s.setdefault(k, v)
    return s


def save_settings(s: dict[str, Any]) -> None:
    _ensure_dirs()
    with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
        json.dump(s, f, indent=2, ensure_ascii=False)


def load_state() -> dict[str, Any]:
    _ensure_dirs()
    if not STATE_PATH.exists():
        return {
            "last_wake_at": None,
            "last_scan_at": None,
            "last_decision": None,
            "last_error": None,
            "channels": {},
            "wake_count": 0,
            "skip_due_to_running": 0,
            "skip_due_to_debounce": 0,
            "trigger_failed_count": 0,
        }
    with open(STATE_PATH, encoding="utf-8") as f:
        return json.load(f)


def save_state(s: dict[str, Any]) -> None:
    _ensure_dirs()
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(s, f, indent=2, ensure_ascii=False)


def load_users_cache() -> dict[str, Any]:
    _ensure_dirs()
    if not USERS_CACHE_PATH.exists():
        return {"users": {}}
    with open(USERS_CACHE_PATH, encoding="utf-8") as f:
        return json.load(f)


def save_users_cache(c: dict[str, Any]) -> None:
    _ensure_dirs()
    with open(USERS_CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(c, f, indent=2, ensure_ascii=False)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# -----------------------------------------------------------------------------
# Trigger log
# -----------------------------------------------------------------------------
def log_trigger_event(record: dict[str, Any]) -> None:
    _ensure_dirs()
    record = {"ts": now_iso(), **record}
    with open(TRIGGERS_LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


# -----------------------------------------------------------------------------
# Discord API helpers
# -----------------------------------------------------------------------------
def fetch_recent_messages(channel_id: str, limit: int, timeout: int) -> list[dict]:
    """Fetch the most recent N messages from a channel via Discord API.
    Returns messages in reverse chronological order (newest first)."""
    url = f"https://discord.com/api/v10/channels/{channel_id}/messages"
    resp = requests.get(
        url,
        params={"limit": min(limit, 100)},
        headers=_api_headers(),
        timeout=timeout,
    )
    if resp.status_code == 401:
        raise RuntimeError(f"401 unauthorized for channel {channel_id} — bot token invalid?")
    if resp.status_code == 403:
        # Missing access; skip silently
        return []
    if resp.status_code == 429:
        # Rate limited; back off briefly
        retry = float(resp.headers.get("retry-after", "1"))
        time.sleep(retry)
        return fetch_recent_messages(channel_id, limit, timeout)
    if resp.status_code != 200:
        raise RuntimeError(f"Discord API {resp.status_code} for channel {channel_id}: {resp.text[:200]}")
    return resp.json()


def fetch_member(user_id: str, timeout: int = 10) -> dict | None:
    """Fetch a guild member object. Returns None if the user is not in the
    server or the API returns a non-200. The member object includes:
      - joined_at: ISO8601 timestamp of when they joined this guild
      - user.created_at: ISO8601 timestamp of Discord account creation
      - roles: list of role IDs
    """
    url = f"https://discord.com/api/v10/guilds/{GUILD_ID}/members/{user_id}"
    resp = requests.get(url, headers=_api_headers(), timeout=timeout)
    if resp.status_code == 404:
        return None  # not in guild
    if resp.status_code == 429:
        retry = float(resp.headers.get("retry-after", "1"))
        time.sleep(retry)
        return fetch_member(user_id, timeout)
    if resp.status_code != 200:
        return None
    return resp.json()


# -----------------------------------------------------------------------------
# Trigger / debounce
# -----------------------------------------------------------------------------
def is_cron_running(job_id: str, timeout: int = 5) -> bool:
    """Check if the moderation cron is currently running by inspecting
    `openclaw cron list --json` for `state.runningAtMs`."""
    try:
        result = subprocess.run(
            ["openclaw", "cron", "list", "--json"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode != 0:
            return False  # can't tell — assume not running
        data = json.loads(result.stdout)
        # `data` may be a list of jobs or {"jobs": [...]}; handle both
        jobs = data if isinstance(data, list) else data.get("jobs", [])
        for j in jobs:
            if j.get("id") == job_id or j.get("jobId") == job_id:
                state = j.get("state", {}) or {}
                if state.get("runningAtMs"):
                    return True
                return False
        return False
    except (subprocess.TimeoutExpired, json.JSONDecodeError, Exception) as e:
        # On any error, default to "not running" so we don't accidentally block triggers
        print(f"WARN: is_cron_running check failed: {e}", file=sys.stderr)
        return False


def within_debounce(last_wake_iso: str | None, debounce_minutes: int) -> bool:
    """Return True if the last wake was within `debounce_minutes` of now."""
    if not last_wake_iso:
        return False
    try:
        last = datetime.fromisoformat(last_wake_iso.replace("Z", "+00:00"))
    except ValueError:
        return False
    delta = datetime.now(timezone.utc) - last
    return delta < timedelta(minutes=debounce_minutes)


def trigger_cron(job_id: str, timeout: int = 30) -> subprocess.CompletedProcess:
    """Wake the moderation cron via `openclaw cron run <jobId>`. Enqueue-only,
    no --wait, returns immediately."""
    return subprocess.run(
        ["openclaw", "cron", "run", job_id],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


# -----------------------------------------------------------------------------
# Main scan logic
# -----------------------------------------------------------------------------
def scan(dry_run: bool = False, manual: bool = False) -> dict[str, Any]:
    """Run one scan cycle. Returns a summary dict (also logged).

    The systemd timer fires every 1 min; the script self-throttles
    based on `interval_minutes` from settings (default 5). Manual
    invocations (--manual) bypass the throttle.
    """
    settings = load_settings()
    state = load_state()

    # Self-throttle: skip if last_scan_at is within interval_minutes
    # (unless --manual was passed).
    if not manual and not dry_run:
        last_scan_iso = state.get("last_scan_at")
        interval = settings.get("interval_minutes", 5)
        if last_scan_iso:
            try:
                last = datetime.fromisoformat(last_scan_iso.replace("Z", "+00:00"))
                delta = datetime.now(timezone.utc) - last
                if delta < timedelta(minutes=interval):
                    decision = "throttled"
                    reason = f"last scan {int(delta.total_seconds())}s ago < interval {interval}m (manual/dry-run bypass throttling)"
                    log_trigger_event({
                        "decision": decision,
                        "reason": reason,
                        "manual": manual,
                        "dry_run": dry_run,
                        "channels_scanned": 0,
                        "new_messages_total": 0,
                        "channels_with_activity": [],
                        "new_user_count": 0,
                        "new_users": [],
                        "triggered_at": None,
                        "trigger_result": None,
                    })
                    return {
                        "decision": decision,
                        "reason": reason,
                        "channels_scanned": 0,
                        "new_messages_total": 0,
                        "new_user_count": 0,
                        "new_users": [],
                        "triggered_at": None,
                        "last_wake_at": state.get("last_wake_at"),
                    }
            except (ValueError, TypeError):
                pass

    users_data = load_users_cache()
    users = users_data.setdefault("users", {})

    protected = _all_protected_users()

    new_users: list[dict] = []
    new_messages_total = 0
    channels_scanned = 0
    channels_with_activity: list[str] = []
    error: str | None = None
    decisions = {
        "channels_skipped_403": [],
    }

    for channel_id in settings["channels"]:
        try:
            messages = fetch_recent_messages(
                channel_id,
                limit=settings["lookback_messages"],
                timeout=settings["fetch_timeout_seconds"],
            )
        except RuntimeError as e:
            # Auth failures etc. — record and stop; not safe to keep scanning
            error = str(e)
            break
        except requests.RequestException as e:
            print(f"WARN: channel {channel_id} fetch error: {e}", file=sys.stderr)
            continue

        channels_scanned += 1
        if not messages:
            continue

        # Determine new messages since last_message_id
        last_msg_id = (state.get("channels", {}).get(channel_id, {}) or {}).get("last_message_id")
        # Discord snowflakes are lexicographically sortable as timestamps
        if last_msg_id:
            new_messages = [m for m in messages if m["id"] > last_msg_id]
        else:
            # First run for this channel — only consider the most recent message
            new_messages = messages[:1]

        if not new_messages:
            continue

        new_messages_total += len(new_messages)
        channels_with_activity.append(channel_id)

        for msg in new_messages:
            author = msg.get("author", {}) or {}
            author_id = str(author.get("id", ""))
            if not author_id:
                continue

            # Update users cache
            if author_id not in users:
                users[author_id] = {
                    "username": author.get("username", "?"),
                    "global_name": author.get("global_name"),
                    "bot": bool(author.get("bot", False)),
                    "first_seen_at": now_iso(),
                    "message_count": 0,
                    "last_seen_at": now_iso(),
                    "last_channel_id": channel_id,
                }
            users[author_id]["message_count"] = users[author_id].get("message_count", 0) + 1
            users[author_id]["last_seen_at"] = now_iso()
            users[author_id]["last_channel_id"] = channel_id
            if not users[author_id].get("bot"):
                users[author_id]["username"] = author.get("username", users[author_id].get("username", "?"))
                users[author_id]["global_name"] = author.get("global_name", users[author_id].get("global_name"))

            # Skip bots
            if author.get("bot"):
                continue
            # Skip protected users
            if author_id in protected:
                continue

            # Check if this counts as a "new user" trigger.
            # Logic (per ban_authority_v2 spec): ≤ 5 prior messages OR
            # Discord account age < 30d. When member data is available,
            # we prefer the joined_at / account_created_at signal over
            # the message-count heuristic (which is unreliable on a
            # fresh cache). Member data is fetched once per user and
            # cached in users_cache.json.
            msg_count = users[author_id].get("message_count", 0)
            is_low_history = msg_count <= settings["new_user_max_history"]
            has_member_info = "joined_at" in users[author_id] or "account_created_at" in users[author_id]

            if not has_member_info:
                # One-time member fetch to get joined_at + account_created_at.
                # Note: Discord's guild member response does NOT include
                # user.created_at directly; we decode it from the user ID
                # (Discord snowflakes encode the creation timestamp in the
                # high bits). See https://discord.com/developers/docs/reference#snowflakes
                member = fetch_member(author_id, timeout=settings["fetch_timeout_seconds"])
                if member:
                    users[author_id]["joined_at"] = member.get("joined_at")
                    user_obj = member.get("user", {}) or {}
                    user_id_str = user_obj.get("id") or author_id
                    try:
                        ts_ms = (int(user_id_str) >> 22) + 1420070400000
                        users[author_id]["account_created_at"] = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).isoformat()
                    except (ValueError, TypeError):
                        users[author_id]["account_created_at"] = None
                    users[author_id]["roles"] = member.get("roles", [])
                    users[author_id]["member_fetched_at"] = now_iso()
                # else: member fetch failed (user not in guild, etc.) —
                # we'll fall through to the message-count heuristic

            # Decide: is this a new user for trigger purposes?
            joined_at = users[author_id].get("joined_at")
            account_created = users[author_id].get("account_created_at")
            is_new = False
            new_reason = None

            if joined_at is not None or account_created is not None:
                # We have member data — trust it.
                if joined_at:
                    try:
                        joined = datetime.fromisoformat(joined_at.replace("Z", "+00:00"))
                        age_days = (datetime.now(timezone.utc) - joined).days
                        if age_days <= settings["new_user_max_account_age_days"]:
                            is_new = True
                            new_reason = f"recent_joiner(joined {age_days}d ago <= {settings['new_user_max_account_age_days']}d)"
                    except (ValueError, TypeError):
                        pass
                if not is_new and account_created:
                    try:
                        created = datetime.fromisoformat(account_created.replace("Z", "+00:00"))
                        age_days = (datetime.now(timezone.utc) - created).days
                        if age_days <= settings["new_user_max_account_age_days"]:
                            is_new = True
                            new_reason = f"new_account(created {age_days}d ago <= {settings['new_user_max_account_age_days']}d)"
                    except (ValueError, TypeError):
                        pass
                if not is_new:
                    new_reason = f"long_time_user(msg_count={msg_count}, joined={joined_at}, account_created={account_created})"
            else:
                # No member data — fall back to message-count heuristic
                if is_low_history:
                    is_new = True
                    new_reason = f"low_history(msg_count={msg_count} <= {settings['new_user_max_history']}, no_member_data)"

            if is_new:
                new_users.append({
                    "user_id": author_id,
                    "username": author.get("username", "?"),
                    "channel_id": channel_id,
                    "message_id": msg["id"],
                    "message_count_in_server": msg_count,
                    "joined_at": joined_at,
                    "account_created_at": account_created,
                    "trigger_reason": new_reason,
                    "content_preview": (msg.get("content") or "")[:200],
                })

        # Update last_message_id for this channel (highest id seen)
        newest_id = max(m["id"] for m in new_messages)
        state.setdefault("channels", {})[channel_id] = {
            "last_message_id": newest_id,
            "last_scanned_at": now_iso(),
            "messages_seen_this_run": len(new_messages),
        }

    # Decision: wake the cron?
    decision = "no_new_user_activity"
    reason: str | None = None
    triggered_at: str | None = None
    trigger_result: dict | None = None

    if error:
        decision = "error"
        reason = error
    elif new_users:
        if dry_run or not settings.get("auto_trigger", True):
            decision = "skipped_dry_run" if dry_run else "skipped_auto_trigger_off"
            reason = "new users found but trigger suppressed by settings/dry-run"
        elif is_cron_running(settings["moderation_cron_job_id"]):
            decision = "skipped_due_to_running"
            reason = f"moderation cron {settings['moderation_cron_job_id']} is currently running"
            state["skip_due_to_running"] = state.get("skip_due_to_running", 0) + 1
        elif within_debounce(state.get("last_wake_at"), settings["debounce_minutes"]):
            last = state.get("last_wake_at")
            decision = "skipped_due_to_debounce"
            reason = f"last wake at {last} within {settings['debounce_minutes']}m debounce window"
            state["skip_due_to_debounce"] = state.get("skip_due_to_debounce", 0) + 1
        else:
            # Fire the trigger
            result = trigger_cron(settings["moderation_cron_job_id"])
            if result.returncode == 0:
                decision = "triggered"
                triggered_at = now_iso()
                state["last_wake_at"] = triggered_at
                state["wake_count"] = state.get("wake_count", 0) + 1
                trigger_result = {
                    "returncode": result.returncode,
                    "stdout": (result.stdout or "").strip()[:500],
                }
            else:
                decision = "trigger_failed"
                reason = f"openclaw cron run exited {result.returncode}: {(result.stderr or '').strip()[:300]}"
                state["trigger_failed_count"] = state.get("trigger_failed_count", 0) + 1

    # Update state
    state["last_scan_at"] = now_iso()
    state["last_decision"] = decision
    state["last_error"] = error
    save_state(state)
    save_users_cache(users_data)

    # Log the trigger event
    log_trigger_event({
        "decision": decision,
        "reason": reason,
        "manual": manual,
        "dry_run": dry_run,
        "channels_scanned": channels_scanned,
        "new_messages_total": new_messages_total,
        "channels_with_activity": channels_with_activity,
        "new_user_count": len(new_users),
        "new_users": new_users,
        "triggered_at": triggered_at,
        "trigger_result": trigger_result,
    })

    return {
        "decision": decision,
        "reason": reason,
        "channels_scanned": channels_scanned,
        "new_messages_total": new_messages_total,
        "new_user_count": len(new_users),
        "new_users": new_users,
        "triggered_at": triggered_at,
        "last_wake_at": state.get("last_wake_at"),
    }


# -----------------------------------------------------------------------------
# CLI subcommands
# -----------------------------------------------------------------------------
def cmd_scan(args):
    result = scan(dry_run=args.dry_run, manual=args.manual)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result["decision"] != "error" else 1


def cmd_status(args):
    state = load_state()
    settings = load_settings()
    users_data = load_users_cache()
    print(json.dumps({
        "last_scan_at": state.get("last_scan_at"),
        "last_wake_at": state.get("last_wake_at"),
        "last_decision": state.get("last_decision"),
        "last_error": state.get("last_error"),
        "wake_count": state.get("wake_count", 0),
        "skip_due_to_running": state.get("skip_due_to_running", 0),
        "skip_due_to_debounce": state.get("skip_due_to_debounce", 0),
        "trigger_failed_count": state.get("trigger_failed_count", 0),
        "channels_tracked": len(state.get("channels", {})),
        "users_cached": len(users_data.get("users", {})),
        "settings": {
            "interval_minutes": settings.get("interval_minutes"),
            "debounce_minutes": settings.get("debounce_minutes"),
            "auto_trigger": settings.get("auto_trigger"),
            "channel_count": len(settings.get("channels", [])),
            "moderation_cron_job_id": settings.get("moderation_cron_job_id"),
        },
    }, indent=2, ensure_ascii=False))
    return 0


def cmd_settings(args):
    settings = load_settings()
    if args.settings_action == "get":
        print(json.dumps(settings, indent=2, ensure_ascii=False))
        return 0
    if args.settings_action == "set":
        if not args.key:
            print("ERROR: 'set' requires a --key", file=sys.stderr)
            return 2
        if args.value is None:
            print("ERROR: 'set' requires --value", file=sys.stderr)
            return 2
        # Coerce value type based on existing setting
        existing = settings.get(args.key)
        if isinstance(existing, bool):
            new_val = args.value.lower() in ("1", "true", "yes", "on")
        elif isinstance(existing, int):
            try:
                new_val = int(args.value)
            except ValueError:
                print(f"ERROR: --value {args.value!r} is not an int (existing key is int)", file=sys.stderr)
                return 2
        elif isinstance(existing, list):
            # Comma-separated
            new_val = [v.strip() for v in args.value.split(",") if v.strip()]
        else:
            new_val = args.value
        old = settings.get(args.key)
        settings[args.key] = new_val
        save_settings(settings)
        print(json.dumps({"key": args.key, "old": old, "new": new_val}, indent=2))
        return 0
    parser_settings(args)  # unreachable; argparse handles help
    return 0


def cmd_triggers(args):
    if not TRIGGERS_LOG_PATH.exists():
        print("[]")
        return 0
    lines = TRIGGERS_LOG_PATH.read_text(encoding="utf-8").splitlines()
    if args.limit:
        lines = lines[-args.limit:]
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0


def cmd_users(args):
    users_data = load_users_cache()
    users = users_data.get("users", {})
    items = list(users.items())
    if args.sort == "messages":
        items.sort(key=lambda kv: kv[1].get("message_count", 0), reverse=True)
    elif args.sort == "last_seen":
        items.sort(key=lambda kv: kv[1].get("last_seen_at", ""), reverse=True)
    elif args.sort == "first_seen":
        items.sort(key=lambda kv: kv[1].get("first_seen_at", ""))
    if args.limit:
        items = items[:args.limit]
    out = [{"user_id": uid, **info} for uid, info in items]
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0


def cmd_users_update_summary(args):
    """Update the LLM-generated community-standing summary for a user.
    Called by the moderation cron after it judges the user's messages.
    Persists to data/moderation_state/users_cache.json.
    """
    if not args.id or not args.summary:
        print("ERROR: --id and --summary required", file=sys.stderr)
        return 2
    users_data = load_users_cache()
    users = users_data.setdefault("users", {})
    uid = args.id
    if uid not in users:
        users[uid] = {
            "username": args.username or "?",
            "first_seen_at": now_iso(),
            "message_count": 0,
            "last_seen_at": now_iso(),
        }
    users[uid]["summary"] = args.summary
    users[uid]["summary_updated_at"] = now_iso()
    if args.username:
        users[uid]["username"] = args.username
    save_users_cache(users_data)
    log_trigger_event({
        "decision": "user_summary_updated",
        "user_id": uid,
        "username": users[uid].get("username"),
        "summary_length": len(args.summary),
    })
    print(json.dumps({
        "success": True,
        "user_id": uid,
        "summary": args.summary,
        "summary_updated_at": users[uid]["summary_updated_at"],
    }, indent=2, ensure_ascii=False))
    return 0


def cmd_trigger_cron(args):
    """Force-wake the moderation cron via `openclaw cron run <jobId>`.
    Bypasses the debounce. Updates last_wake_at in state."""
    job_id = args.job_id or load_settings().get("moderation_cron_job_id", DEFAULT_MODERATION_CRON_JOB_ID)
    result = trigger_cron(job_id)
    if result.returncode != 0:
        print(json.dumps({
            "success": False,
            "error": f"openclaw cron run exit {result.returncode}",
            "stderr": (result.stderr or "").strip()[:500],
        }, indent=2))
        return 1
    state = load_state()
    state["last_wake_at"] = now_iso()
    state["wake_count"] = state.get("wake_count", 0) + 1
    save_state(state)
    print(json.dumps({
        "success": True,
        "job_id": job_id,
        "triggered_at": state["last_wake_at"],
        "wake_count": state["wake_count"],
        "stdout": (result.stdout or "").strip()[:300],
    }, indent=2, ensure_ascii=False))
    return 0


def cmd_archive(args):
    """Read moderation_actions_archive.jsonl, optionally filtered to user actions.
    Default: ban + ban_user + timeout + timeout_user (the Banned/Timeout sub-tab view).
    The wider filter covers both live actions (ban_user, timeout_user) and
    backfilled actions (ban, timeout) from the earlier Lenny-era warnings."""
    archive_path = SELENA_PROJECT / "data" / "moderation_actions_archive.jsonl"
    if not archive_path.exists():
        print("[]")
        return 0
    # Both legacy and new action names for backwards-compat
    BAN_ACTIONS = {"ban", "ban_user"}
    TIMEOUT_ACTIONS = {"timeout", "timeout_user"}
    if args.action == "ban":
        action_filter = BAN_ACTIONS
    elif args.action == "timeout":
        action_filter = TIMEOUT_ACTIONS
    else:  # both
        action_filter = BAN_ACTIONS | TIMEOUT_ACTIONS
    entries = []
    with open(archive_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            if e.get("action") in action_filter:
                entries.append(e)
    entries.sort(key=lambda e: e.get("ts", ""), reverse=True)
    if args.limit:
        entries = entries[:args.limit]
    print(json.dumps({"count": len(entries), "entries": entries}, indent=2, ensure_ascii=False))
    return 0


def cmd_policies(_args):
    """Read selena-project/data/moderation_policies.md (display copy of moderation policies)."""
    policies_path = DATA / "moderation_policies.md"
    if not policies_path.exists():
        print(json.dumps({"error": "policies file not found", "path": str(policies_path)}, indent=2))
        return 1
    stat = policies_path.stat()
    content = policies_path.read_text(encoding="utf-8")
    print(json.dumps({
        "path": str(policies_path.relative_to(SELENA_PROJECT)),
        "last_updated": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
        "length": len(content),
        "content": content,
    }, indent=2, ensure_ascii=False))
    return 0


def cmd_docs(_args):
    """Read selena-project/docs/moderation.md (architecture doc)."""
    docs_path = SELENA_PROJECT / "docs" / "moderation.md"
    if not docs_path.exists():
        print(json.dumps({"error": "docs file not found", "path": str(docs_path)}, indent=2))
        return 1
    stat = docs_path.stat()
    content = docs_path.read_text(encoding="utf-8")
    print(json.dumps({
        "path": str(docs_path.relative_to(SELENA_PROJECT)),
        "last_updated": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
        "length": len(content),
        "content": content,
    }, indent=2, ensure_ascii=False))
    return 0


# -----------------------------------------------------------------------------
# Pending actions (dry-run / unexecuted moderation actions)
# Stored at data/pending_actions.json. Each action is a row the user can
# Apply (execute via moderation_check.py --execute), Delete (remove from
# list), or Edit (change reason). The list survives across page reloads
# and is the staging area for the Run Now sub-tab.
# -----------------------------------------------------------------------------
def _load_pending():
    if not PENDING_ACTIONS_PATH.exists():
        return {"actions": []}
    try:
        with open(PENDING_ACTIONS_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {"actions": []}

def _save_pending(d):
    _ensure_dirs()
    with open(PENDING_ACTIONS_PATH, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=2, ensure_ascii=False)

def _new_pending_id():
    """Monotonic-ish ID. Uses timestamp + counter for stability across sessions."""
    base = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    # Find existing IDs with the same timestamp base and bump the counter
    d = _load_pending()
    same = [a for a in d.get("actions", []) if a.get("id", "").startswith(f"pa-{base}-")]
    n = len(same) + 1
    return f"pa-{base}-{n:03d}"

def cmd_pending(args):
    if args.pending_action == "list":
        return cmd_pending_list(args)
    if args.pending_action == "add":
        return cmd_pending_add(args)
    if args.pending_action == "edit":
        return cmd_pending_edit(args)
    if args.pending_action == "delete":
        return cmd_pending_delete(args)
    if args.pending_action == "apply":
        return cmd_pending_apply(args)
    if args.pending_action == "urgent-list":
        return cmd_pending_urgent_list(args)
    if args.pending_action == "urgent-add":
        return cmd_pending_urgent_add(args)
    if args.pending_action == "urgent-clear":
        return cmd_pending_urgent_clear(args)
    if args.pending_action == "urgent-trim":
        return cmd_pending_urgent_trim(args)
    return 2

def cmd_pending_list(_args):
    d = _load_pending()
    out = {
        "count": len(d.get("actions", [])),
        "actions": d.get("actions", []),
    }
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0

def cmd_pending_add(args):
    if not args.target_user_id:
        print(json.dumps({"error": "--target-user-id required"}, indent=2))
        return 2
    if not args.action or args.action not in ("ban_user", "timeout_user"):
        print(json.dumps({"error": "--action must be ban_user or timeout_user"}, indent=2))
        return 2
    if args.action == "timeout_user" and not args.duration:
        print(json.dumps({"error": "--duration required for timeout_user"}, indent=2))
        return 2
    if not args.reason:
        print(json.dumps({"error": "--reason required"}, indent=2))
        return 2
    d = _load_pending()
    action = {
        "id": _new_pending_id(),
        "action": args.action,
        "target_user_id": args.target_user_id,
        "target_username": args.target_username or "",
        "duration": args.duration or "",
        "reason": args.reason,
        "source": args.source or "manual",
        "created_at": now_iso(),
        "created_by": args.created_by or "manual",
    }
    d.setdefault("actions", []).append(action)
    _save_pending(d)
    log_trigger_event({
        "decision": "pending_action_added",
        "action_id": action["id"],
        "target_user_id": action["target_user_id"],
        "target_username": action["target_username"],
        "action_type": action["action"],
        "duration": action["duration"],
        "source": action["source"],
    })
    print(json.dumps({"success": True, "action": action}, indent=2, ensure_ascii=False))
    return 0

def cmd_pending_edit(args):
    if not args.id or not args.reason:
        print(json.dumps({"error": "--id and --reason required"}, indent=2))
        return 2
    d = _load_pending()
    target = next((a for a in d.get("actions", []) if a.get("id") == args.id), None)
    if not target:
        print(json.dumps({"error": f"no pending action with id {args.id}"}, indent=2))
        return 2
    target["reason"] = args.reason
    target["updated_at"] = now_iso()
    _save_pending(d)
    log_trigger_event({
        "decision": "pending_action_edited",
        "action_id": args.id,
    })
    print(json.dumps({"success": True, "action": target}, indent=2, ensure_ascii=False))
    return 0

def cmd_pending_delete(args):
    if not args.id:
        print(json.dumps({"error": "--id required"}, indent=2))
        return 2
    d = _load_pending()
    before = len(d.get("actions", []))
    d["actions"] = [a for a in d.get("actions", []) if a.get("id") != args.id]
    after = len(d.get("actions", []))
    if before == after:
        print(json.dumps({"error": f"no pending action with id {args.id}"}, indent=2))
        return 2
    _save_pending(d)
    log_trigger_event({
        "decision": "pending_action_deleted",
        "action_id": args.id,
    })
    print(json.dumps({"success": True, "deleted": args.id, "remaining": after}, indent=2, ensure_ascii=False))
    return 0

def cmd_pending_apply(args):
    """Execute one or all pending actions. Writes a temp actions file in the
    shape moderation_check.py --execute expects, calls it, and removes the
    pending row(s) on success.
    """
    d = _load_pending()
    actions = d.get("actions", [])
    if args.id:
        targets = [a for a in actions if a.get("id") == args.id]
        if not targets:
            print(json.dumps({"error": f"no pending action with id {args.id}"}, indent=2))
            return 2
    elif args.all:
        targets = list(actions)
        if not targets:
            print(json.dumps({"info": "no pending actions to apply"}))
            return 0
    else:
        print(json.dumps({"error": "specify --id or --all"}, indent=2))
        return 2
    # Build the actions file the moderation_check.py CLI expects
    actions_for_file = []
    for a in targets:
        entry = {"type": a["action"], "user_id": a["target_user_id"], "reason": a["reason"]}
        if a.get("duration"):
            entry["duration"] = a["duration"]
        if a.get("delete_message_seconds"):
            entry["delete_message_seconds"] = a["delete_message_seconds"]
        actions_for_file.append(entry)
    # Write to a temp file
    import tempfile
    fd, tmp_path = tempfile.mkstemp(suffix=".json", prefix="pending_actions_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"actions": actions_for_file}, f, indent=2, ensure_ascii=False)
        # Call moderation_check.py --execute (LIVE mode — no --dry-run)
        result = subprocess.run(
            ["python3", str(SELENA_PROJECT / "scripts" / "moderation_check.py"),
             "--execute", tmp_path],
            capture_output=True, text=True, timeout=120, cwd=str(SELENA_PROJECT)
        )
        log_trigger_event({
            "decision": "pending_action_apply",
            "ids": [a["id"] for a in targets],
            "exit_code": result.returncode,
            "stdout": (result.stdout or "")[:300],
            "stderr": (result.stderr or "")[:300],
        })
        if result.returncode != 0:
            print(json.dumps({
                "success": False,
                "error": f"execute-actions exit {result.returncode}",
                "stderr": (result.stderr or "")[-500:],
            }, indent=2))
            return 1
        # On success, remove the applied rows
        applied_ids = {a["id"] for a in targets}
        d["actions"] = [a for a in actions if a.get("id") not in applied_ids]
        _save_pending(d)
        print(json.dumps({
            "success": True,
            "applied": [a["id"] for a in targets],
            "remaining": len(d["actions"]),
            "stdout": (result.stdout or "")[:500],
        }, indent=2, ensure_ascii=False))
        return 0
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

def cmd_pending_urgent_list(_args):
    """List the auto-execute lane. Each entry shows whether it was already
    auto-executed (live) or is a dry-run preview."""
    if not PENDING_URGENT_PATH.exists():
        print(json.dumps({"count": 0, "actions": []}, indent=2))
        return 0
    try:
        with open(PENDING_URGENT_PATH, encoding="utf-8") as f:
            d = json.load(f)
    except (json.JSONDecodeError, OSError):
        print(json.dumps({"count": 0, "actions": []}, indent=2))
        return 0
    if not isinstance(d, dict):
        d = {"actions": []}
    out = {
        "count": len(d.get("actions", [])),
        "actions": d.get("actions", []),
    }
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0

def cmd_pending_urgent_add(args):
    """Manually add an urgent action (for testing or out-of-band decisions)."""
    if not args.target_user_id:
        print(json.dumps({"error": "--target-user-id required"}, indent=2))
        return 2
    if not args.action or args.action not in ("ban_user", "timeout_user"):
        print(json.dumps({"error": "--action must be ban_user or timeout_user"}, indent=2))
        return 2
    if args.action == "timeout_user" and not args.duration:
        print(json.dumps({"error": "--duration required for timeout_user"}, indent=2))
        return 2
    if not args.reason:
        print(json.dumps({"error": "--reason required"}, indent=2))
        return 2
    d = _load_pending_urgent()
    action = {
        "id": _new_pending_urgent_id(),
        "action": args.action,
        "target_user_id": args.target_user_id,
        "target_username": args.target_username or "",
        "duration": args.duration or "",
        "reason": args.reason,
        "source": args.source or "manual-urgent",
        "dry_run": False,
        "auto_executed": False,  # manual add: not yet executed
        "created_at": now_iso(),
        "created_by": args.created_by or "manual",
    }
    d.setdefault("actions", []).append(action)
    _save_pending_urgent(d)
    log_trigger_event({
        "decision": "urgent_action_added",
        "action_id": action["id"],
        "target_user_id": action["target_user_id"],
        "target_username": action["target_username"],
        "action_type": action["action"],
        "duration": action["duration"],
        "source": action["source"],
    })
    print(json.dumps({"success": True, "action": action}, indent=2, ensure_ascii=False))
    return 0

def cmd_pending_urgent_clear(args):
    """Clear the urgent lane log. Only for manual maintenance — does NOT
    un-execute anything; the archive log retains the full audit trail."""
    if not args.id:
        print(json.dumps({"error": "--id required to clear a specific entry"}, indent=2))
        return 2
    d = _load_pending_urgent()
    before = len(d.get("actions", []))
    d["actions"] = [a for a in d.get("actions", []) if a.get("id") != args.id]
    after = len(d.get("actions", []))
    if before == after:
        print(json.dumps({"error": f"no urgent action with id {args.id}"}, indent=2))
        return 2
    _save_pending_urgent(d)
    log_trigger_event({
        "decision": "urgent_action_cleared",
        "action_id": args.id,
    })
    print(json.dumps({"success": True, "cleared": args.id, "remaining": after}, indent=2, ensure_ascii=False))
    return 0


# Path for the trimmed urgent entries (append-only JSONL, matches the pattern
# of moderation_actions_archive.jsonl). One JSON object per line, never
# rewritten. Created on first trim if it doesn't exist.
PENDING_URGENT_ARCHIVE_PATH = DATA / "pending_actions_urgent_archive.jsonl"


def _parse_iso(ts: str | None):
    """Parse an ISO-8601 timestamp into an aware datetime in UTC.
    Returns None if the string is missing or unparseable (treated as 'keep')."""
    if not ts or not isinstance(ts, str):
        return None
    try:
        # Python's fromisoformat handles both 'Z' and '+00:00' on 3.11+;
        # be defensive for older runtimes too.
        s = ts.strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def cmd_pending_urgent_trim(args):
    """Trim entries older than N days from data/pending_actions_urgent.json.

    Behavior:
      - Reads the live urgent lane.
      - For every action with created_at older than (now - max_age_days),
        appends the FULL record (plus trimmed_at) to
        data/pending_actions_urgent_archive.jsonl (one JSON per line).
      - Removes those entries from the live list and rewrites the file.
      - Live file is left in place even if it ends up empty; the file
        always exists with shape {"actions": []}.
      - --dry-run: compute the cut list, do NOT write anything, return
        a preview. Useful for `--dry-run --verbose`-style debugging.

    Why: pending_actions_urgent.json is append-only and would grow forever
    (one entry per insta-ban / auto-timeout). After 30 days the live list
    is no longer operationally useful, but the audit trail matters — so
    archive, don't delete. (see todo 18a427e8, 2026-06-04 follow-up)
    """
    max_age_days = int(getattr(args, "max_age_days", 30))
    if max_age_days < 1:
        print(json.dumps({"error": "--max-age-days must be >= 1"}, indent=2))
        return 2
    dry_run = bool(getattr(args, "dry_run", False))

    d = _load_pending_urgent()
    actions = d.get("actions", []) if isinstance(d, dict) else []
    before = len(actions)

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=max_age_days)

    keep = []
    trim = []
    for a in actions:
        ts = _parse_iso(a.get("created_at"))
        if ts is None:
            # No parseable created_at → keep (don't risk deleting records
            # we can't reason about). Logged in the response for visibility.
            keep.append(a)
            continue
        if ts < cutoff:
            trim.append(a)
        else:
            keep.append(a)

    trimmed_count = len(trim)
    kept_count = len(keep)

    if dry_run:
        print(json.dumps({
            "dry_run": True,
            "max_age_days": max_age_days,
            "cutoff": cutoff.isoformat(),
            "before": before,
            "trimmed_count": trimmed_count,
            "kept_count": kept_count,
            "archive_path": str(PENDING_URGENT_ARCHIVE_PATH),
            "sample_trimmed_ids": [a.get("id") for a in trim[:5]],
        }, indent=2, ensure_ascii=False))
        return 0

    if trimmed_count == 0:
        # No-op fast path. Still log the run for visibility, but don't
        # touch the file (idempotent and cheap).
        print(json.dumps({
            "success": True,
            "trimmed_count": 0,
            "kept_count": kept_count,
            "max_age_days": max_age_days,
            "cutoff": cutoff.isoformat(),
            "archive_path": str(PENDING_URGENT_ARCHIVE_PATH),
        }, indent=2, ensure_ascii=False))
        return 0

    # Append trimmed entries to the archive (JSONL, one per line, append-only).
    # Create parent dirs if missing (DATA is the data/ dir; the file lives
    # alongside the live file, so no extra dirs needed, but be safe).
    try:
        PENDING_URGENT_ARCHIVE_PATH.parent.mkdir(parents=True, exist_ok=True)
        trimmed_at = now_iso()
        with open(PENDING_URGENT_ARCHIVE_PATH, "a", encoding="utf-8") as f:
            for a in trim:
                rec = {
                    "trimmed_at": trimmed_at,
                    "max_age_days": max_age_days,
                    "cutoff": cutoff.isoformat(),
                    **a,
                }
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError as e:
        # Best-effort: don't lose the data we just identified. Bail with
        # a clear error so the caller (the moderation cron) can decide
        # whether to retry or alert.
        print(json.dumps({
            "error": f"failed to write archive: {e}",
            "archive_path": str(PENDING_URGENT_ARCHIVE_PATH),
        }, indent=2))
        return 1

    # Rewrite the live file with only the kept entries.
    d["actions"] = keep
    _save_pending_urgent(d)

    log_trigger_event({
        "decision": "urgent_actions_trimmed",
        "trimmed_count": trimmed_count,
        "kept_count": kept_count,
        "max_age_days": max_age_days,
        "archive_path": str(PENDING_URGENT_ARCHIVE_PATH),
    })

    print(json.dumps({
        "success": True,
        "trimmed_count": trimmed_count,
        "kept_count": kept_count,
        "max_age_days": max_age_days,
        "cutoff": cutoff.isoformat(),
        "archive_path": str(PENDING_URGENT_ARCHIVE_PATH),
    }, indent=2, ensure_ascii=False))
    return 0

def _load_pending_urgent():
    if not PENDING_URGENT_PATH.exists():
        return {"actions": []}
    try:
        with open(PENDING_URGENT_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {"actions": []}

def _save_pending_urgent(d):
    _ensure_dirs()
    with open(PENDING_URGENT_PATH, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=2, ensure_ascii=False)

def _new_pending_urgent_id():
    base = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    d = _load_pending_urgent()
    same = [a for a in d.get("actions", []) if a.get("id", "").startswith(f"pu-{base}-")]
    n = len(same) + 1
    return f"pu-{base}-{n:03d}"

def cmd_discord_health(args):
    """CLI equivalent of GET /api/discord/health. Use this from a shell,
    the watchdog, or any external monitor — it doesn't require a password
    because it runs in a fresh process and checks the same local state the
    HTTP endpoint would return.

    Modes (default is fast, no side effects):
      (default)        Fast config check: token in env? token in openclaw
                       config? DISCORD_ENABLED not "false"? No side effects.
                       Exits 0 if config is sane, 1 if not, 2 on error.
      --start          Actually start the notifier (calls Discord API,
                       ~2-3s, spawns the discord.py worker). Slower but
                       reports the live `status()` from the worker. Use
                       this for end-to-end debugging.
      --json           Output machine-readable JSON to stdout (one line,
                       suitable for piping). Exit code is the same.
      --verbose        Show all config fields even when healthy.
    """
    import os
    from pathlib import Path as _Path

    # ── Fast config check (always runs first, no side effects) ──
    token_in_env = bool(os.getenv('DISCORD_BOT_TOKEN', '').strip())
    enabled_env = (os.getenv('DISCORD_ENABLED', 'true').strip().lower()
                   not in ('false', '0', 'no', 'off'))

    openclaw_cfg_path = _Path(os.path.expanduser('~/.openclaw/openclaw.json'))
    token_in_openclaw = False
    openclaw_loaded = False
    openclaw_err = None
    if openclaw_cfg_path.exists():
        try:
            with openclaw_cfg_path.open() as f:
                cfg = json.load(f)
            openclaw_loaded = True
            # Token can live in several places; check the most common.
            d = cfg.get('channels', {}).get('discord', {})
            if d.get('bot_token') or d.get('token'):
                token_in_openclaw = True
            # Some configs nest it differently.
            if not token_in_openclaw:
                if cfg.get('discord', {}).get('bot_token'):
                    token_in_openclaw = True
        except Exception as e:
            openclaw_err = str(e)

    config_ok = (token_in_env or token_in_openclaw) and enabled_env

    result = {
        'config': {
            'token_in_env': token_in_env,
            'token_in_openclaw': token_in_openclaw,
            'discord_enabled_env': enabled_env,
            'openclaw_config_loaded': openclaw_loaded,
            'openclaw_config_path': str(openclaw_cfg_path),
        },
        'start_invoked': bool(args.start),
        'live': {},  # populated when --start is used
    }
    if openclaw_err:
        result['config']['openclaw_error'] = openclaw_err

    exit_code = 0 if config_ok else 1

    # ── Optional: actually start the notifier ──
    if args.start:
        try:
            sys.path.insert(0, str(SELENA_PROJECT / 'code'))
            from discord_client import DiscordNotifier
            n = DiscordNotifier()
            started = n.start()
            st = n.status()
            result['live'] = {
                'start_returned': started,
                'enabled': st.get('enabled', False),
                'default_channel_id': st.get('default_channel_id'),
                'post_count': st.get('post_count', 0),
                'last_error': st.get('last_error'),
            }
            n.stop()
            if not started or not st.get('enabled'):
                exit_code = 1
                result['ok'] = False
            else:
                result['ok'] = True
        except Exception as e:
            result['live'] = {'error': str(e)}
            exit_code = 2
    else:
        result['ok'] = config_ok

    # ── Output ──
    if args.json:
        # One line of JSON for machine consumers.
        print(json.dumps(result, ensure_ascii=False))
    else:
        if result.get('ok'):
            print('OK: discord notifier config is sane')
            if args.verbose:
                cfg = result['config']
                print(f'  token_in_env:        {cfg["token_in_env"]}')
                print(f'  token_in_openclaw:   {cfg["token_in_openclaw"]}')
                print(f'  DISCORD_ENABLED:     {cfg["discord_enabled_env"]}')
                if cfg.get('openclaw_error'):
                    print(f'  openclaw error:      {cfg["openclaw_error"]}')
        else:
            print('NOT HEALTHY: discord notifier config check failed')
            cfg = result['config']
            reasons = []
            if not (cfg['token_in_env'] or cfg['token_in_openclaw']):
                reasons.append('no bot token found in DISCORD_BOT_TOKEN or ~/.openclaw/openclaw.json')
            if not cfg['discord_enabled_env']:
                reasons.append('DISCORD_ENABLED is set to false/0/no/off')
            if cfg.get('openclaw_error'):
                reasons.append(f'openclaw config read error: {cfg["openclaw_error"]}')
            for r in reasons:
                print(f'  - {r}')
        if result.get('live'):
            print('Live status (--start):')
            print(json.dumps(result['live'], indent=2, ensure_ascii=False))

    return exit_code


def cmd_drift_check(_args):
    """Compare the cron prompt's policy block against moderation_policies.md.
    Per Arcurus 2026-06-04: when you change the rules in the cron prompt,
    also update policies.md (and vice versa). This CLI flags drift.

    The cron prompt's policy block is everything between the "BAN POLICY"
    marker and the "SECURITY / SCOPE" marker. The display file is the
    full content of moderation_policies.md.

    Exit code: 0 = no drift, 1 = drift detected, 2 = error.
    """
    import difflib
    import re
    # 1. Find the cron prompt
    jobs_path = Path(os.path.expanduser("~/.openclaw/cron/jobs.json"))
    if not jobs_path.exists():
        print(json.dumps({"error": "cron jobs.json not found", "path": str(jobs_path)}, indent=2))
        return 2
    try:
        with open(jobs_path, encoding="utf-8") as f:
            jobs = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(json.dumps({"error": f"could not parse jobs.json: {e}"}, indent=2))
        return 2
    # Find the moderation cron job
    settings = load_settings()
    target_id = settings.get("moderation_cron_job_id", DEFAULT_MODERATION_CRON_JOB_ID)
    job = next((j for j in jobs.get("jobs", []) if j.get("id") == target_id), None)
    if not job:
        print(json.dumps({"error": f"cron job {target_id} not found in jobs.json"}, indent=2))
        return 2
    prompt = job.get("payload", {}).get("message", "")
    if not prompt:
        print(json.dumps({"error": f"cron job {target_id} has no payload message"}, indent=2))
        return 2

    # 2. Extract the policy block from the cron prompt
    # Look for "BAN POLICY" through "SECURITY / SCOPE"
    policy_start_markers = ["BAN POLICY \u2014 PER ARCURUS", "BAN POLICY"]
    policy_end_markers = ["SECURITY / SCOPE \u2014 NON-NEGOTIABLE", "SECURITY / SCOPE"]
    cron_policy = None
    for start_marker in policy_start_markers:
        s = prompt.find(start_marker)
        if s != -1:
            for end_marker in policy_end_markers:
                e = prompt.find(end_marker, s)
                if e != -1 and e > s:
                    cron_policy = prompt[s:e].strip()
                    break
            if cron_policy:
                break
    if not cron_policy:
        print(json.dumps({
            "error": "could not locate policy block in cron prompt",
            "hint": "looking for markers: " + ", ".join(policy_start_markers)
        }, indent=2))
        return 2

    # 3. Read policies.md
    policies_path = DATA / "moderation_policies.md"
    if not policies_path.exists():
        print(json.dumps({"error": f"policies file not found: {policies_path}"}, indent=2))
        return 2
    policies_text = policies_path.read_text(encoding="utf-8").strip()

    # 4. Compute a simple drift check
    def normalize(s):
        s = re.sub(r"\s+", " ", s).strip()
        return s
    cron_norm = normalize(cron_policy)
    policies_norm = normalize(policies_text)

    drift = False
    diff_lines = []
    if cron_norm == policies_norm:
        drift = False
    else:
        drift = True
        cron_lines = cron_policy.splitlines()
        policies_lines = policies_text.splitlines()
        for line in difflib.unified_diff(
            policies_lines, cron_lines,
            fromfile="moderation_policies.md",
            tofile="cron prompt (ban policy block)",
            lineterm="",
            n=2,
        ):
            diff_lines.append(line)

    cron_words = cron_norm.split()
    policies_words = policies_norm.split()
    only_in_cron = sorted(set(cron_words) - set(policies_words))
    only_in_policies = sorted(set(policies_words) - set(cron_words))

    report = {
        "drift_detected": drift,
        "cron_policy_chars": len(cron_policy),
        "policies_md_chars": len(policies_text),
        "cron_policy_words": len(cron_words),
        "policies_md_words": len(policies_words),
        "only_in_cron_count": len(only_in_cron),
        "only_in_policies_count": len(only_in_policies),
        "only_in_cron_sample": only_in_cron[:30],
        "only_in_policies_sample": only_in_policies[:30],
        "diff_first_50_lines": diff_lines[:50],
        "cron_job_id": target_id,
        "policies_md_path": str(policies_path.relative_to(SELENA_PROJECT)),
        "cron_jobs_path": str(jobs_path),
        "policies_md_mtime": datetime.fromtimestamp(policies_path.stat().st_mtime, tz=timezone.utc).isoformat(),
        "checked_at": now_iso(),
    }
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 1 if drift else 0


# -----------------------------------------------------------------------------
# argparse
# -----------------------------------------------------------------------------
def main():
    p = argparse.ArgumentParser(description="Selena Project — Discord new-user scanner + moderation cron trigger")
    sub = p.add_subparsers(dest="cmd", required=True)

    p_scan = sub.add_parser("scan", help="Run a scan cycle")
    p_scan.add_argument("--manual", action="store_true", help="Manual mode (records manual trigger in log)")
    p_scan.add_argument("--dry-run", action="store_true", help="Don't wake the cron, just log the decision")
    p_scan.set_defaults(func=cmd_scan)

    sub.add_parser("status", help="Show last scan/wake/decision summary").set_defaults(func=cmd_status)

    p_settings = sub.add_parser("settings", help="View or modify settings")
    settings_sub = p_settings.add_subparsers(dest="settings_action", required=True)
    p_sg = settings_sub.add_parser("get", help="Show all settings")
    p_sg.set_defaults(func=cmd_settings)
    p_ss = settings_sub.add_parser("set", help="Set a key")
    p_ss.add_argument("--key", required=True)
    p_ss.add_argument("--value", required=True)
    p_ss.set_defaults(func=cmd_settings)

    p_tr = sub.add_parser("triggers", help="Show recent trigger events")
    p_tr.add_argument("--limit", type=int, default=20)
    p_tr.set_defaults(func=cmd_triggers)

    p_us = sub.add_parser("users", help="Show cached users (or update a user's summary)")
    # Top-level args for backwards-compat: `users --limit 50` works as `users list --limit 50`
    p_us.add_argument("--limit", type=int, default=50)
    p_us.add_argument("--sort", choices=["messages", "last_seen", "first_seen"], default="last_seen")
    us_sub = p_us.add_subparsers(dest="users_action")
    p_us_list = us_sub.add_parser("list", help="List cached users (same as `users` with no subcommand)")
    p_us_list.add_argument("--limit", type=int, default=50)
    p_us_list.add_argument("--sort", choices=["messages", "last_seen", "first_seen"], default="last_seen")
    p_us_list.set_defaults(func=cmd_users)
    # Backwards-compat: `users` with no subcommand → list
    p_us.set_defaults(func=cmd_users)
    p_us_sum = us_sub.add_parser("update-summary", help="Update a user's community-standing summary (called by moderation cron)")
    p_us_sum.add_argument("--id", required=True, help="Discord user ID")
    p_us_sum.add_argument("--summary", required=True, help="1-2 sentence community-standing summary")
    p_us_sum.add_argument("--username", help="Optional: update cached username at the same time")
    p_us_sum.set_defaults(func=cmd_users_update_summary)

    p_tc = sub.add_parser("trigger-cron", help="Force-wake the moderation cron (bypasses debounce)")
    p_tc.add_argument("--job-id", help="Override the moderation cron job id (default: from settings)")
    p_tc.set_defaults(func=cmd_trigger_cron)

    p_ar = sub.add_parser("archive", help="Read moderation_actions_archive.jsonl (filtered to user actions)")
    p_ar.add_argument("--limit", type=int, default=100)
    p_ar.add_argument("--action", choices=["ban", "timeout", "both"], default="both",
                       help="Filter: ban, timeout, or both (default: both — covers legacy 'ban'/'timeout' AND new 'ban_user'/'timeout_user' names)")
    p_ar.set_defaults(func=cmd_archive)

    sub.add_parser("policies", help="Read moderation_policies.md (display copy of policies)").set_defaults(func=cmd_policies)
    sub.add_parser("docs", help="Read docs/moderation.md (architecture doc)").set_defaults(func=cmd_docs)
    sub.add_parser("drift-check", help="Compare the cron prompt's policy block against moderation_policies.md and report drift").set_defaults(func=cmd_drift_check)
    p_dh = sub.add_parser("discord-health",
        help="Check the Discord notifier health (CLI equivalent of GET /api/discord/health). "
             "Use from a shell, the watchdog, or any external monitor. "
             "Returns exit 0 if healthy, 1 if not, 2 on error. Use --json for machine output.")
    p_dh.add_argument('--start', action='store_true',
        help="Actually start the notifier (slow, ~2-3s, has side effects). Default is fast config-only check.")
    p_dh.add_argument('--json', action='store_true', help="Output JSON (one line) instead of human-readable text.")
    p_dh.add_argument('--verbose', action='store_true', help="Show all config fields even when healthy.")
    p_dh.set_defaults(func=cmd_discord_health)

    p_pa = sub.add_parser("pending", help="Manage pending (unexecuted) moderation actions")
    pa_sub = p_pa.add_subparsers(dest="pending_action", required=True)
    p_pa_list = pa_sub.add_parser("list", help="List all pending actions")
    p_pa_list.set_defaults(func=cmd_pending)
    p_pa_add = pa_sub.add_parser("add", help="Add a pending action (does NOT execute)")
    p_pa_add.add_argument("--target-user-id", required=True, help="Discord user ID")
    p_pa_add.add_argument("--target-username", help="Display name (for UI)")
    p_pa_add.add_argument("--action", required=True, choices=["ban_user", "timeout_user"])
    p_pa_add.add_argument("--duration", help="Duration (e.g. 1h, 24h, 7d) - required for timeout_user")
    p_pa_add.add_argument("--reason", required=True, help="Reason / policy cite")
    p_pa_add.add_argument("--source", help="Source label (default: manual)")
    p_pa_add.add_argument("--created-by", help="Who/what created this (default: manual)")
    p_pa_add.set_defaults(func=cmd_pending)
    p_pa_edit = pa_sub.add_parser("edit", help="Edit the reason of a pending action")
    p_pa_edit.add_argument("--id", required=True, help="Pending action ID")
    p_pa_edit.add_argument("--reason", required=True, help="New reason text")
    p_pa_edit.set_defaults(func=cmd_pending)
    p_pa_del = pa_sub.add_parser("delete", help="Delete a pending action (does NOT execute)")
    p_pa_del.add_argument("--id", required=True, help="Pending action ID")
    p_pa_del.set_defaults(func=cmd_pending)
    p_pa_apply = pa_sub.add_parser("apply", help="Execute a pending action (or all)")
    p_pa_apply.add_argument("--id", help="Apply this specific action")
    p_pa_apply.add_argument("--all", action="store_true", help="Apply all pending actions")
    p_pa_apply.set_defaults(func=cmd_pending)
    pa_sub.add_parser("urgent-list", help="List the auto-execute lane (urgent actions)").set_defaults(func=cmd_pending)
    p_pa_uadd = pa_sub.add_parser("urgent-add", help="Manually add an urgent action (does NOT execute)")
    p_pa_uadd.add_argument("--target-user-id", required=True)
    p_pa_uadd.add_argument("--target-username", help="Display name (for UI)")
    p_pa_uadd.add_argument("--action", required=True, choices=["ban_user", "timeout_user"])
    p_pa_uadd.add_argument("--duration", help="Required for timeout_user")
    p_pa_uadd.add_argument("--reason", required=True)
    p_pa_uadd.add_argument("--source", help="Source label (default: manual-urgent)")
    p_pa_uadd.add_argument("--created-by", help="Who created it (default: manual)")
    p_pa_uadd.set_defaults(func=cmd_pending)
    p_pa_uclear = pa_sub.add_parser("urgent-clear", help="Remove an entry from the urgent lane (does NOT un-execute)")
    p_pa_uclear.add_argument("--id", required=True, help="Urgent action ID to remove")
    p_pa_uclear.set_defaults(func=cmd_pending)
    p_pa_utrim = pa_sub.add_parser("urgent-trim", help="Archive + remove urgent-lane entries older than N days (default 30). Idempotent; safe to run from a cron.")
    p_pa_utrim.add_argument("--max-age-days", type=int, default=30, help="Max age in days before trim (default 30)")
    p_pa_utrim.add_argument("--dry-run", action="store_true", help="Compute the cut list but do NOT write anything")
    p_pa_utrim.set_defaults(func=cmd_pending)

    args = p.parse_args()
    sys.exit(args.func(args) or 0)


if __name__ == "__main__":
    main()
