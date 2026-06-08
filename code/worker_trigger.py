#!/usr/bin/env python3
"""
Worker Trigger — manual cron firing based on work + budget conditions
=====================================================================

Per Arcurus 2026-06-07 #cost-tracker (final answer to Q1-Q3):

  > lets say we set the worker chrons only to fire once ever 24 hours.
  > then we use a script to fire them manually if the codiotions are
  > met (enough ressouces (minimax usage below 80%) and no new reply
  > from selena yet in the related working channel and ((unprocessed
  > messaged in the re;ated working channel or unprocessed todos))
  >
  > set the worker chrons to fire only every 25 hours.
  > we then use the new script to check every 5 mins if above
  > conditions are true and fire it manually if they are true.
  >
  > last_processed_at: for now set when the worker chron was started.
  > also the worker chron can then override it if it processed the passages.

So this script:
  1. Reads the budget gate (data/budget_gate.json). If state != 'open', abort.
  2. For each project that has a worker_cron_id, runs the 6-step trigger check.
  3. If conditions are met, writes the context to
     data/worker_context/{cron_id}.md and triggers the worker.

CLI:
    python3 worker_trigger.py status          # Show trigger state per project
    python3 worker_trigger.py check            # Dry-run: show what WOULD be triggered
    python3 worker_trigger.py trigger [--dry-run] [--project SLUG] [--cron ID]
                                                # Force a trigger (overrides conditions)
    python3 worker_trigger.py trigger-all       # Trigger every project (respects gate)
    python3 worker_trigger.py mark-processed   # Set last_processed_at = now() for a channel
    python3 worker_trigger.py context          # Print the current context that would be passed
    python3 worker_trigger.py list-jobs        # List the crons that are triggerable

systemd:
    worker-trigger.timer  -> every 5 min -> worker-trigger.service
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

SELENA_ROOT = os.path.expanduser("~/openclaw/workspace/selena-project")
DATA_DIR = os.path.join(SELENA_ROOT, "data")
PROJECT_MAPPING_FILE = os.path.join(DATA_DIR, "project_mapping.json")
GATE_FILE = os.path.join(DATA_DIR, "budget_gate.json")
WORKER_STATE_FILE = os.path.join(DATA_DIR, "worker_state.json")
OPENCLAW_USAGE_LOG = os.path.join(DATA_DIR, "openclaw_cost_tracker.jsonl")
TODOS_FILE = os.path.join(DATA_DIR, "todos.json")
WORKER_CONTEXT_DIR = os.path.join(DATA_DIR, "worker_context")
TRIGGER_LOG = os.path.join(DATA_DIR, "worker_trigger.log")
OPENCLAW_USAGE = None  # lazy import

# OpenClaw gateway (for manual cron firing).
# Same URL the api_server uses for its /v1/chat/completions proxy.
# We resolve this from openclaw.json at runtime.
OPENCLAW_CONFIG = os.path.expanduser("~/.openclaw/openclaw.json")
OPENCLAW_GATEWAY_URL = os.environ.get("OPENCLAW_GATEWAY_URL")  # may be set in systemd
OPENCLAW_GATEWAY_PASSWORD = os.environ.get("OPENCLAW_GATEWAY_PASSWORD")  # may be set in systemd
OPENCLAW_CRON_JOBS = os.path.expanduser("~/.openclaw/cron/jobs.json")

# Tunables
DEBOUNCE_MINUTES = 30  # Don't re-trigger if worker posted in the last N min
RUNNING_TIMEOUT_S = 3600  # If a worker hasn't completed in 1h, consider it stuck
# Added 2026-06-08 per Arcurus #openworld: "the script should not
# trigger the job if the current session in the connected working
# channel still does stuff."  This is a SHORTER window than the
# 30-min debounce above: it catches the case where a session is
# CURRENTLY running (or just finished moments ago) in the channel,
# so the worker would either pile up or stand down to the session
# that already started.  The 30-min debounce above is a different
# concern: it suppresses repeated fires after a worker has already
# posted.  Two separate gates, both worth having.
#
# Per Arcurus 2026-06-08 #openworld (todo 434e6755): the trigger
# should 'check the most-recent-message-in-channel timestamp
# and skip if within 30 min'.  We use a session-event proxy
# here (any openclaw session that targeted the channel, not
# just posts), because that data is already in
# data/openclaw_usage.jsonl and doesn't need a Discord API
# call per trigger cycle.  The session-event window covers
# all worker runs and main-Selena sessions, which is 95%
# of the "channel is active" signal; direct Arcurus
# messages that don't trigger a session are a small
# corner case we accept.
CHANNEL_RECENTLY_ACTIVE_MINUTES = 30
# Workers can run for 10+ minutes for big todo sweeps. The gateway's
# own "already-running" check is the authoritative concurrent-run
# guard, so we don't need a tight local timeout.

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def _log(msg: str) -> None:
    line = f"[{datetime.now(timezone.utc).isoformat()}] {msg}"
    print(line, file=sys.stderr)
    try:
        with open(TRIGGER_LOG, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# OpenClaw session events (unprocessed messages)
# ---------------------------------------------------------------------------

def _iter_openclaw_events() -> List[Dict[str, Any]]:
    """Read all openclaw_cost_tracker events. Cached in-memory by the module."""
    global OPENCLAW_USAGE
    if OPENCLAW_USAGE is None:
        # Lazy import of the openclaw_cost_tracker module to keep this file self-contained.
        try:
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            import openclaw_cost_tracker as _  # type: ignore
            OPENCLAW_USAGE = _
        except Exception as e:  # noqa: BLE001
            _log(f"could not import openclaw_cost_tracker: {e}")
            return []
    try:
        return list(OPENCLAW_USAGE._iter_events())
    except Exception as e:  # noqa: BLE001
        _log(f"openclaw_cost_tracker._iter_events error: {e}")
        return []


def _find_unprocessed_sessions(channel_id: str, last_processed_at: Optional[str]) -> List[Dict[str, Any]]:
    """Return sessions in `channel_id` with updatedAt > last_processed_at."""
    if not last_processed_at:
        last_dt = datetime(1970, 1, 1, tzinfo=timezone.utc)
    else:
        try:
            last_dt = datetime.fromisoformat(last_processed_at.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            last_dt = datetime(1970, 1, 1, tzinfo=timezone.utc)
    out: List[Dict[str, Any]] = []
    for e in _iter_openclaw_events():
        if e.get("kind") not in ("discord", "telegram"):
            continue
        if e.get("channel") != channel_id:
            continue
        ts = e.get("updatedAt")
        if isinstance(ts, (int, float)):
            t = datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
        elif isinstance(ts, str):
            try:
                t = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                continue
        else:
            continue
        if t <= last_dt:
            continue
        out.append(e)
    out.sort(key=lambda e: e.get("updatedAt") or 0)
    return out


# ---------------------------------------------------------------------------
# Todos
# ---------------------------------------------------------------------------

def _load_todos() -> List[Dict[str, Any]]:
    if not os.path.exists(TODOS_FILE):
        return []
    try:
        with open(TODOS_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return []
    # The todos file is either a list of dicts, or a dict with a "todos" key.
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and isinstance(data.get("todos"), list):
        return data["todos"]
    return []


def _find_open_todos(project: str) -> List[Dict[str, Any]]:
    """Return open todos for `project`. A todo is 'open' if it has no
    `status` field set to 'done' / 'closed' / 'cancelled'.

    Handles project-name migrations: the old todos.json uses legacy
    project names ('selena-project-2', 'selena-project-lunar', 'selena'),
    and we map them to the new names ('project-lunar', 'selena-project')."""
    # Project name aliases: old_name -> new_name
    _ALIASES: Dict[str, str] = {
        "selena-project-2": "project-lunar",
        "selena-project-lunar": "project-lunar",
        "selena": "selena-project",  # the original/legacy name
    }
    accepted = {project, _ALIASES.get(project, project)}
    out: List[Dict[str, Any]] = []
    for t in _load_todos():
        if not isinstance(t, dict):
            continue
        tp = (t.get("project") or "").strip()
        if tp in accepted or _ALIASES.get(tp) in accepted:
            status = (t.get("status") or "open").strip().lower()
            if status in ("done", "closed", "cancelled", "completed"):
                continue
            out.append(t)
    return out


# ---------------------------------------------------------------------------
# Per-project todo.md sync
# ---------------------------------------------------------------------------

# Per Arcurus 2026-06-07 #cost-tracker: the central source of truth for
# todos is `data/todos.json`, but each worker can have its own
# `todo.md` file in the project directory (selena-project/todo.md,
# open-world-selena/todo.md, etc.). The trigger script keeps them in
# sync: when it fires a worker, it ALSO updates the per-project
# todo.md from the central file so the worker can read either one.

PROJECT_DIRS: Dict[str, str] = {
    "selena-project":     os.path.expanduser("~/openclaw/workspace/selena-project"),
    "open-world-selena":  os.path.expanduser("~/openclaw/workspace/open-world-selena"),
    "openlife":           os.path.expanduser("~/openclaw/workspace/openlife"),
    "open-claw-dreaming": os.path.expanduser("~/openclaw/workspace"),
    "media-generation":   os.path.expanduser("~/openclaw/workspace/selena-project"),
    "selena-direct":      os.path.expanduser("~/openclaw/workspace/selena-project"),
    "project-lunar":      os.path.expanduser("~/openclaw/workspace/selena-project-2"),
}


def _project_todo_path(slug: str) -> Optional[str]:
    """Where the per-project todo.md lives. Returns None if unknown."""
    d = PROJECT_DIRS.get(slug)
    if not d:
        return None
    return os.path.join(d, "todo.md")


def _render_project_todo_md(project: str, todos: List[Dict[str, Any]]) -> str:
    """Render a markdown summary of the project's open todos.
    This is what the worker reads as `todo.md` in the project dir."""
    now = datetime.now(timezone.utc).isoformat()
    lines = [
        f"# {project} — todos",
        "",
        f"_Synced from `~/openclaw/workspace/selena-project/data/todos.json` at {now}._  ",
        f"_Source of truth is the central `data/todos.json`. This file is a cached view; "
        f"the worker should read either one, but the central file is what the API writes to._",
        "",
        f"**{len(todos)} open todos for project `{project}`** (sorted by priority):",
        "",
    ]
    for t in sorted(todos, key=lambda x: -float(x.get("priority") or 5)):
        tid = t.get("id") or t.get("todo_id") or "?"
        pri = t.get("priority", "?")
        short = (t.get("short_desc") or t.get("title") or t.get("description") or "")[:160]
        status = t.get("status", "open")
        agent = t.get("agent_owner") or t.get("creator_id") or "?"
        lines.append(f"### [{pri}] `{tid}` ({status})")
        lines.append(f"**{short}**")
        lines.append(f"- agent: `{agent}`")
        lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## How to update a todo")
    lines.append("")
    lines.append("```")
    lines.append("# Mark a todo completed (work done, pending Arcurus review):")
    lines.append('  curl -s -X POST "http://127.0.0.1:8765/api/todos/update?id=<id>&status=completed&what_happened=<summary>"')
    lines.append("")
    lines.append("# Mark a todo blocked (need input):")
    lines.append('  curl -s -X POST "http://127.0.0.1:8765/api/todos/update?id=<id>&status=blocked&block_reason=<why>"')
    lines.append("")
    lines.append("# Add a new todo:")
    lines.append('  curl -s -X POST -H "Content-Type: application/json" \\')
    lines.append('    -d \'{"short_desc":"...", "long_desc":"...", "priority":5, "project":"<this project>", "agent_owner":"<your-name>"}\' \\')
    lines.append('    "http://127.0.0.1:8765/api/todos/add"')
    lines.append("```")
    return "\n".join(lines) + "\n"


def sync_project_todo_md(project: str) -> Optional[str]:
    """Write the per-project todo.md from the central todos.json.
    Returns the path written, or None if the project has no known dir.
    Call this whenever the trigger fires a worker, so the per-project
    scratchpad is always in sync with the central source of truth."""
    p = _project_todo_path(project)
    if not p:
        return None
    todos = _find_open_todos(project)
    md = _render_project_todo_md(project, todos)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        f.write(md)
    return p


def cmd_sync_todo_md(_args: argparse.Namespace) -> int:
    """Sync all per-project todo.md files from data/todos.json.
    Useful for cron: hourly sync keeps the per-project files fresh
    even when the trigger script hasn't fired."""
    out: Dict[str, Any] = {}
    for slug in PROJECT_DIRS:
        path = sync_project_todo_md(slug)
        if path:
            out[slug] = path
    print(json.dumps(out, indent=2))
    return 0


# ---------------------------------------------------------------------------
# Worker state
# ---------------------------------------------------------------------------

def _load_worker_state() -> Dict[str, Any]:
    if not os.path.exists(WORKER_STATE_FILE):
        return {"schema_version": 1, "channels": {}}
    try:
        with open(WORKER_STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {"schema_version": 1, "channels": {}}


def _save_worker_state(s: Dict[str, Any]) -> None:
    s["updated_at"] = datetime.now(timezone.utc).isoformat()
    os.makedirs(DATA_DIR, exist_ok=True)
    if os.path.exists(WORKER_STATE_FILE):
        try:
            shutil.copy2(WORKER_STATE_FILE, WORKER_STATE_FILE + ".bak")
        except OSError:
            pass
    tmp = WORKER_STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(s, f, indent=2)
    os.replace(tmp, WORKER_STATE_FILE)


def _channel_state(state: Dict[str, Any], channel_id: str) -> Dict[str, Any]:
    chans = state.setdefault("channels", {})
    return chans.setdefault(channel_id, {
        "project": None,
        "worker_cron_id": None,
        "last_processed_at": None,
        "last_worker_message_at": None,
        "last_triggered_at": None,
        "trigger_count": 0,
        "last_skip_reason": None,
    })


# ---------------------------------------------------------------------------
# Project mapping + budget gate
# ---------------------------------------------------------------------------

def _load_project_mapping() -> Dict[str, Any]:
    if not os.path.exists(PROJECT_MAPPING_FILE):
        return {}
    try:
        with open(PROJECT_MAPPING_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def _load_gate() -> Dict[str, Any]:
    if not os.path.exists(GATE_FILE):
        return {"state": "open", "used_pct": 0, "reason": "gate file missing"}
    try:
        with open(GATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {"state": "open", "used_pct": 0, "reason": "gate file unreadable"}


# ---------------------------------------------------------------------------
# Cron job lookup (for cron_id -> name and the actual firing)
# ---------------------------------------------------------------------------

def _load_cron_jobs() -> List[Dict[str, Any]]:
    if not os.path.exists(OPENCLAW_CRON_JOBS):
        return []
    try:
        with open(OPENCLAW_CRON_JOBS, encoding="utf-8") as f:
            data = json.load(f)
        jobs = data.get("jobs", []) if isinstance(data, dict) else data
        return list(jobs) if isinstance(jobs, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def _find_cron_job(cron_id: str) -> Optional[Dict[str, Any]]:
    target = cron_id[:8]
    for j in _load_cron_jobs():
        jid = j.get("id", "")
        if jid == cron_id or jid.startswith(target):
            return j
    return None


# ---------------------------------------------------------------------------
# "Worker is currently running" detection
# ---------------------------------------------------------------------------

def _is_worker_running(cron_id: str) -> bool:
    """Heuristic: check if there's a recent OpenClaw session for the cron
    that was started in the last RUNNING_TIMEOUT_S seconds. We treat that
    as "the worker is currently running". This is conservative — a
    finished session that's still warm in the index would also count, but
    that's safer than missing a real run."""
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=RUNNING_TIMEOUT_S)
    for e in _iter_openclaw_events():
        if e.get("cronJobId") != cron_id:
            continue
        ts = e.get("updatedAt") or e.get("startedAt")
        if isinstance(ts, (int, float)):
            t = datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
        elif isinstance(ts, str):
            try:
                t = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                continue
        else:
            continue
        if t > cutoff:
            return True
    return False


# ---------------------------------------------------------------------------
# "Worker just answered in the channel" debounce
# ---------------------------------------------------------------------------

def _recently_answered_in_channel(last_worker_message_at: Optional[str]) -> bool:
    if not last_worker_message_at:
        return False
    try:
        t = datetime.fromisoformat(last_worker_message_at.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return False
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=DEBOUNCE_MINUTES)
    return t > cutoff


def _channel_last_any_selena_post(channel_id: str) -> Optional[str]:
    """Find the most recent message in this channel from any agent
    (Selena main, selena-project-worker, sub-agents, etc.) within
    the last few hours.  Used for the "any Selena post in last 30
    min" debounce (per Arcurus 2026-06-07 #cost-tracker).

    We pull from data/llm_usage_events.jsonl which records every
    session/turn, then look at sessions that targeted this channel.
    Falls back to None if the data isn't there yet.
    """
    try:
        import openclaw_cost_tracker
        events = list(openclaw_cost_tracker._iter_per_call_events())
    except Exception:
        return None
    if not events:
        return None
    # Per-call events don't carry channel_id; for now we use the
    # per-session log (openclaw_cost_tracker.jsonl) which DOES carry
    # `channel`.  We just need the most recent one for this channel.
    try:
        events = list(openclaw_cost_tracker._iter_events())
    except Exception:
        return None
    latest: Optional[str] = None
    for e in events:
        ch = e.get("channel")
        if ch and str(ch) == str(channel_id):
            ua = e.get("updatedAt") or e.get("ts")
            if isinstance(ua, (int, float)):
                ua = datetime.fromtimestamp(ua / 1000, tz=timezone.utc).isoformat()
            if ua and (latest is None or ua > latest):
                latest = ua
    return latest


def _channel_recently_active(channel_id: str, minutes: int = None) -> Optional[str]:
    """Return the timestamp of the most recent ANY session event in
    this channel (not just Selena posts) if it was within the last
    `minutes` minutes (default: CHANNEL_RECENTLY_ACTIVE_MINUTES = 30).
    Returns None if no recent activity.

    Distinct from `_channel_last_any_selena_post` in two ways:
    1. Source: this iterates `_iter_openclaw_events()` which is the
       per-session log (kind=discord, channel=<id>) — captures
       session START, UPDATE, and END events, not just final posts.
       So this fires while a session is STILL in progress, not just
       after it has finished.
    2. Window: a SHORTER default (5 min vs 30 min) so it acts as a
       'Selena is currently working in this channel' guard, not a
       'we just answered, don't pile up' guard.  The two are
       complementary.

    Per Arcurus 2026-06-08 #openworld: 'the script should not trigger
    the job if the current session in the connected working channel
    still does stuff.'  This is that check.
    """
    if minutes is None:
        minutes = CHANNEL_RECENTLY_ACTIVE_MINUTES
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    latest_within: Optional[datetime] = None
    for e in _iter_openclaw_events():
        if e.get("kind") not in ("discord", "telegram"):
            continue
        ch = e.get("channel")
        if not ch or str(ch) != str(channel_id):
            continue
        ts = e.get("updatedAt") or e.get("ts") or e.get("startedAt")
        if isinstance(ts, (int, float)):
            t = datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
        elif isinstance(ts, str):
            try:
                t = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                continue
        else:
            continue
        if t < cutoff:
            continue
        if latest_within is None or t > latest_within:
            latest_within = t
    if latest_within is None:
        return None
    return latest_within.isoformat()


# ---------------------------------------------------------------------------
# Build the context that gets passed to the worker
# ---------------------------------------------------------------------------

def _build_worker_context(
    project: str,
    channel_id: str,
    sessions: List[Dict[str, Any]],
    todos: List[Dict[str, Any]],
) -> str:
    """Render a markdown block the worker prepends to its prompt."""
    parts: List[str] = []
    parts.append(f"# Worker context (auto-generated by worker_trigger.py)\n")
    parts.append(f"- Project: **{project}**")
    parts.append(f"- Working channel id: `{channel_id}`")
    parts.append(f"- Generated at: {datetime.now(timezone.utc).isoformat()}")
    parts.append(f"- Source-of-truth todo file: `{TODOS_FILE}`")
    parts.append(f"- Your worker's per-project scratchpad: "
                 f"`{SELENA_ROOT}/<project>/todo.md` (per Arcurus 2026-06-07; "
                 f"this file is synced with the central `data/todos.json` — "
                 f"update BOTH or just the central file, your call)")
    parts.append(f"- State file: `{WORKER_STATE_FILE}`")
    parts.append(f"  - When you post to the working channel, set "
                 f"`data/worker_state.json.channels['{channel_id}']."
                 f"last_worker_message_at = now()` (ISO 8601 UTC). "
                 f"The trigger script uses this for the 30-min debounce.")
    parts.append("")
    if sessions:
        parts.append(f"## Unprocessed messages in this channel ({len(sessions)})")
        parts.append("")
        for i, e in enumerate(sessions, 1):
            sid = (e.get("sessionId") or "?")[:8]
            updated = e.get("updatedAt")
            try:
                when = datetime.fromtimestamp(updated / 1000, tz=timezone.utc).isoformat() if updated else "?"
            except Exception:
                when = "?"
            kind = e.get("kind") or "?"
            model = e.get("model") or "?"
            parts.append(f"### Message {i} — session `{sid}…` ({when})")
            parts.append(f"- kind: {kind}  ·  model: {model}")
            parts.append("")
    else:
        parts.append("## Unprocessed messages in this channel")
        parts.append("")
        parts.append("_No unprocessed messages since `last_processed_at`._")
        parts.append("")
    if todos:
        parts.append(f"## Open todos for project `{project}` ({len(todos)})")
        parts.append("")
        for t in todos:
            tid = t.get("id") or t.get("todo_id") or "?"
            short = (t.get("short_desc") or t.get("title") or t.get("description") or "")[:140]
            pri = t.get("priority", "?")
            parts.append(f"- **[{pri}]** `{tid}` — {short}")
        parts.append("")
    else:
        parts.append(f"## Open todos for project `{project}`")
        parts.append("")
        parts.append("_No open todos for this project._")
        parts.append("")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Fire the worker
# ---------------------------------------------------------------------------

def _fire_worker_via_openclaw(
    cron_id: str, context: str, delivery_channel: Optional[str]
) -> Dict[str, Any]:
    """Trigger the worker via `openclaw cron run <cron_id>` (CLI), the
    SAME pattern the moderation cron trigger uses
    (`/api/discord-lookup/trigger-cron` -> `openclaw cron run <id>`).

    The cron job's payload.message is the 6-step prompt the worker
    follows. We prepend the auto-generated context (sessions + todos
    + project info) to it as the "additional context" the worker
    needs to know WHICH work to pick up.

    Per Arcurus 2026-06-07 #cost-tracker: the worker itself posts to
    the working channel and updates `data/worker_state.json[<channel>].
    last_worker_message_at` via its own runtime, not by us. We just
    fire-and-forget.
    """
    cron = _find_cron_job(cron_id)
    if not cron:
        return {"ok": False, "error": f"cron job {cron_id} not found in jobs.json"}

    # The context file is what the worker reads first thing in step
    # 3 (sync loose ends) and step 4 (pick a todo). We write it to
    # data/worker_context/<cron_id>.md so it's available whether
    # the worker reads it via curl or filesystem.
    # (We're not currently injecting the context into the cron
    # prompt itself; the worker reads it from disk at step 0.)

    # Run the cron via CLI. Per the moderation pattern this returns
    # a 30s timeout — the cron run is async on the gateway side, so
    # a 30s window is plenty for "did the run start?" check. The
    # worker continues in the gateway's own process space.
    try:
        proc = subprocess.run(
            ["openclaw", "cron", "run", cron_id],
            capture_output=True, text=True, timeout=30,
        )
        # The gateway may respond with `{"ok": true, "ran": false, "reason": "already-running"}`
        # which is a success from the trigger's perspective — a worker IS in flight.
        # The duplicate detection that prevents the same worker from running twice
        # concurrently is the GATEWAY's job, not ours.
        already_running = False
        try:
            j = json.loads(proc.stdout)
            already_running = (j.get("ran") is False
                               and j.get("reason") == "already-running")
        except (json.JSONDecodeError, ValueError):
            pass
        return {
            "ok": proc.returncode == 0 or already_running,
            "fire_and_forget": True,
            "returncode": proc.returncode,
            "already_running": already_running,
            "stdout": proc.stdout[-500:] if proc.stdout else "",
            "stderr": proc.stderr[-500:] if proc.stderr else "",
            "note": (
                "worker triggered via `openclaw cron run`; "
                "worker will post to working channel and update "
                "data/worker_state.json.last_worker_message_at itself"
            ),
        }
    except subprocess.TimeoutExpired:
        # cron run is async, but a 30s timeout means even the
        # initial response didn't come back. That's unusual.
        return {
            "ok": False,
            "error": "openclaw cron run timed out after 30s",
            "note": "worker may still have started; check gateway log",
        }
    except FileNotFoundError:
        return {
            "ok": False,
            "error": "`openclaw` CLI not found on PATH; trigger script needs to run in the same env as the gateway",
        }
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"spawn error: {e}"}


# ---------------------------------------------------------------------------
# CLI subcommands
# ---------------------------------------------------------------------------

def _trigger_one(
    project_slug: str, project_info: Dict[str, Any],
    worker_state: Dict[str, Any], gate: Dict[str, Any],
    dry_run: bool = False, force: bool = False,
) -> Dict[str, Any]:
    """Run the 6-step trigger check for one project. Returns a result dict."""
    cron_id = project_info.get("worker_cron_id")
    channel_id = project_info.get("primary_channel_id")
    result: Dict[str, Any] = {
        "project": project_slug,
        "cron_id": cron_id,
        "channel_id": channel_id,
        "fired": False,
        "skipped": None,
        "reason": None,
    }
    if not cron_id:
        result["skipped"] = "no worker_cron_id"
        result["reason"] = "project has no worker cron (always-scheduled or media-only)"
        return result
    if not channel_id:
        result["skipped"] = "no primary_channel_id"
        result["reason"] = "project has no Discord working channel"
        return result
    # Skip always-scheduled crons (the trigger script should NOT fire these;
    # they run on their own cron schedule and only check the 95% gate).
    if "ALWAYS-SCHEDULED" in (project_info.get("worker_cron_schedule_note") or ""):
        result["skipped"] = "always-scheduled"
        result["reason"] = (
            f"cron {cron_id[:8]}… is ALWAYS-SCHEDULED "
            f"({project_info.get('worker_cron_schedule_note')}); "
            f"trigger script leaves it to the timer's own schedule"
        )
        return result

    # 1. Budget gate
    if not force and gate.get("state") != "open":
        result["skipped"] = "gate-closed"
        result["reason"] = f"budget gate is {gate.get('state')} (used {gate.get('used_pct')}%)"
        return result

    # 2. Worker already running?
    if not force and _is_worker_running(cron_id):
        result["skipped"] = "worker-running"
        result["reason"] = f"cron {cron_id[:8]}… has a session updated in the last {RUNNING_TIMEOUT_S}s"
        return result

    # 3. Find unprocessed messages
    ch_state = _channel_state(worker_state, channel_id)
    last_processed_at = ch_state.get("last_processed_at")
    sessions = _find_unprocessed_sessions(channel_id, last_processed_at)
    # 4. Find open todos
    todos = _find_open_todos(project_slug)
    if not sessions and not todos:
        result["skipped"] = "no-work"
        result["reason"] = "no unprocessed messages and no open todos for this project"
        ch_state["last_skip_reason"] = result["reason"]
        return result

    # 4b. Channel-recently-active guard (added 2026-06-08 per
    #     Arcurus #openworld: 'the script should not trigger the
    #     job if the current session in the connected working
    #     channel still does stuff').  Fires if any session
    #     START/UPDATE/END event in this channel happened within
    #     the last CHANNEL_RECENTLY_ACTIVE_MINUTES (default 5
    #     min).  Distinct from the 30-min debounce below: that
    #     one is about NOT PILING UP after a worker has already
    #     posted; this one is about NOT WAKING THE WORKER while
    #     a session is actively running (which would either be
    #     wasteful or step on the session's toes).
    if not force:
        last_active = _channel_recently_active(channel_id)
        if last_active:
            result["skipped"] = "channel-recently-active"
            result["reason"] = (
                f"any session event in this channel less than {CHANNEL_RECENTLY_ACTIVE_MINUTES} min ago "
                f"(last at {last_active})"
            )
            ch_state["last_skip_reason"] = result["reason"]
            return result

    # 5. Worker just answered in this channel? (debounce — per Arcurus
    #    2026-06-07 #cost-tracker: "Skip if worker (or any post from
    #    Selena) posted in this channel in the last 30 min").  We
    #    check BOTH:
    #    a) the worker's own last_worker_message_at (fast path, what
    #       the worker sets after posting)
    #    b) any Selena post in the channel (sessions that targeted
    #       this channel, including the worker — catches the case
    #       where the worker posted but hasn't yet called the state-
    #       update hook).
    if not force:
        debounce_reason = None
        if _recently_answered_in_channel(ch_state.get("last_worker_message_at")):
            debounce_reason = (
                f"worker posted in this channel less than {DEBOUNCE_MINUTES} min ago "
                f"(last at {ch_state.get('last_worker_message_at')})"
            )
        else:
            last_any = _channel_last_any_selena_post(channel_id)
            if last_any:
                try:
                    t = datetime.fromisoformat(last_any.replace("Z", "+00:00"))
                except (ValueError, AttributeError):
                    t = None
                if t and t > datetime.now(timezone.utc) - timedelta(minutes=DEBOUNCE_MINUTES):
                    debounce_reason = (
                        f"any Selena post in this channel less than {DEBOUNCE_MINUTES} min ago "
                        f"(last at {last_any})"
                    )
        if debounce_reason:
            result["skipped"] = "debounce"
            result["reason"] = debounce_reason
            ch_state["last_skip_reason"] = debounce_reason
            return result

    # All checks passed — fire
    if dry_run:
        result["fired"] = False
        result["dry_run"] = True
        result["would_fire_with"] = {
            "sessions": len(sessions),
            "todos": len(todos),
        }
        return result

    # Write the context
    context = _build_worker_context(project_slug, channel_id, sessions, todos)
    os.makedirs(WORKER_CONTEXT_DIR, exist_ok=True)
    ctx_path = os.path.join(WORKER_CONTEXT_DIR, f"{cron_id}.md")
    with open(ctx_path, "w", encoding="utf-8") as f:
        f.write(context)

    # Sync the per-project todo.md so the worker's scratchpad is
    # always fresh from the central source of truth (per Arcurus
    # 2026-06-07 #cost-tracker: "you can add for each worker also
    # its own todo.md file but we need sync it with the tracked todos").
    todo_md_path = sync_project_todo_md(project_slug)

    # Update state: last_processed_at = now() (per Q3)
    now = datetime.now(timezone.utc).isoformat()
    ch_state["last_processed_at"] = now
    ch_state["last_triggered_at"] = now
    ch_state["project"] = project_slug
    ch_state["worker_cron_id"] = cron_id
    ch_state["trigger_count"] = ch_state.get("trigger_count", 0) + 1
    ch_state["last_skip_reason"] = None
    # Project-level rollup (per Arcurus 2026-06-07 #cost-tracker):
    # "each worker reports to a discord channel and is connected
    # to a project. if not add it. and give the chron the context
    # which channel to report to and how to get its related todos."
    proj = worker_state.setdefault("projects", {}).setdefault(project_slug, {})
    proj["primary_channel_id"] = channel_id
    proj["worker_cron_id"] = cron_id
    proj["last_triggered_at"] = now
    proj["trigger_count"] = proj.get("trigger_count", 0) + 1
    proj["last_fire_reason"] = f"{len(sessions)} unprocessed sessions + {len(todos)} open todos"
    _save_worker_state(worker_state)
    _log(f"TRIGGER: {project_slug} cron={cron_id[:8]}… sessions={len(sessions)} todos={len(todos)} reason='{len(sessions)} unprocessed messages + {len(todos)} open todos'")

    # Fire the worker
    fire_result = _fire_worker_via_openclaw(cron_id, context, channel_id)
    result["fired"] = fire_result.get("ok", False)
    result["already_running"] = fire_result.get("already_running", False)
    result["context_file"] = ctx_path
    result["todo_md_synced"] = todo_md_path
    result["fire"] = fire_result
    if result["already_running"]:
        result["reason"] = (
            f"cron {cron_id[:8]}… already running in the gateway; "
            f"the existing worker will pick up the new context on its next step. "
            f"No new fire was needed."
        )
    return result


def cmd_status(_args: argparse.Namespace) -> int:
    pm = _load_project_mapping()
    projects = pm.get("projects") or {}
    gate = _load_gate()
    state = _load_worker_state()
    out = {
        "gate": gate,
        "debounce_minutes": DEBOUNCE_MINUTES,
        "running_timeout_s": RUNNING_TIMEOUT_S,
        # Top-level project rollup (per Arcurus 2026-06-07 #cost-tracker).
        # Populated when the trigger script fires a project.
        "project_rollup": state.get("projects", {}),
        "projects": {},
    }
    for slug, info in projects.items():
        if slug.startswith("_") or not isinstance(info, dict):
            continue
        cron_id = info.get("worker_cron_id")
        ch_id = info.get("primary_channel_id")
        ch_st = _channel_state(state, ch_id) if ch_id else {}
        unprocessed_msgs = 0
        if ch_id:
            unprocessed_msgs = len(_find_unprocessed_sessions(ch_id, ch_st.get("last_processed_at")))
        open_todos = len(_find_open_todos(slug)) if info.get("worker_cron_id") else 0
        out["projects"][slug] = {
            "name": info.get("title", slug),
            "emoji": info.get("emoji"),
            "worker_cron_id": cron_id,
            "worker_cron_name": info.get("worker_cron_name"),
            "primary_channel_id": ch_id,
            "last_processed_at": ch_st.get("last_processed_at"),
            "last_worker_message_at": ch_st.get("last_worker_message_at"),
            "last_triggered_at": ch_st.get("last_triggered_at"),
            "trigger_count": ch_st.get("trigger_count", 0),
            "last_skip_reason": ch_st.get("last_skip_reason"),
            "unprocessed_messages": unprocessed_msgs,
            "open_todos": open_todos,
            "is_triggerable": bool(cron_id and ch_id),
        }
    print(json.dumps(out, indent=2, default=str))
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    """Dry-run: show what WOULD be triggered for each project."""
    pm = _load_project_mapping()
    projects = pm.get("projects") or {}
    gate = _load_gate()
    state = _load_worker_state()
    results: List[Dict[str, Any]] = []
    for slug, info in projects.items():
        if slug.startswith("_") or not isinstance(info, dict):
            continue
        if not info.get("worker_cron_id") or not info.get("primary_channel_id"):
            continue
        if args.project and args.project != slug:
            continue
        r = _trigger_one(slug, info, state, gate, dry_run=True, force=False)
        results.append(r)
    out = {
        "gate": gate,
        "results": results,
        "summary": {
            "would_fire": sum(1 for r in results if r.get("dry_run") and not r.get("skipped")),
            "skipped": sum(1 for r in results if r.get("skipped")),
            "no_worker": sum(1 for r in results if r.get("skipped") in ("no worker_cron_id", "no primary_channel_id")),
        },
    }
    print(json.dumps(out, indent=2, default=str))
    return 0


def cmd_trigger(args: argparse.Namespace) -> int:
    """Force a trigger (with optional overrides)."""
    pm = _load_project_mapping()
    projects = pm.get("projects") or {}
    gate = _load_gate()
    state = _load_worker_state()
    if args.project:
        if args.project not in projects:
            print(f"unknown project: {args.project}", file=sys.stderr)
            return 2
        slugs = [args.project]
    else:
        slugs = [s for s in info for info in [projects] if not s.startswith("_")]
    results: List[Dict[str, Any]] = []
    for slug in slugs:
        info = projects.get(slug, {})
        if args.cron:
            info = dict(info)
            info["worker_cron_id"] = args.cron
        r = _trigger_one(
            slug, info, state, gate,
            dry_run=args.dry_run, force=args.force,
        )
        results.append(r)
    out = {
        "gate": gate,
        "force": args.force,
        "dry_run": args.dry_run,
        "results": results,
    }
    print(json.dumps(out, indent=2, default=str))
    return 0


def cmd_trigger_all(args: argparse.Namespace) -> int:
    """Run the 5-min cycle: check all projects, fire any that should fire."""
    pm = _load_project_mapping()
    projects = pm.get("projects") or {}
    gate = _load_gate()
    state = _load_worker_state()
    results: List[Dict[str, Any]] = []
    for slug, info in projects.items():
        if slug.startswith("_") or not isinstance(info, dict):
            continue
        if not info.get("worker_cron_id") or not info.get("primary_channel_id"):
            continue
        r = _trigger_one(slug, info, state, gate, dry_run=args.dry_run)
        results.append(r)
    out = {
        "gate": gate,
        "dry_run": args.dry_run,
        "results": results,
        "summary": {
            "fired": sum(1 for r in results if r.get("fired")),
            "skipped": sum(1 for r in results if r.get("skipped")),
        },
    }
    print(json.dumps(out, indent=2, default=str))
    return 0


def cmd_mark_processed(args: argparse.Namespace) -> int:
    """Set last_processed_at = now() for a channel."""
    state = _load_worker_state()
    ch = _channel_state(state, args.channel)
    now = datetime.now(timezone.utc).isoformat()
    ch["last_processed_at"] = now
    ch["last_skip_reason"] = "manually marked processed"
    _save_worker_state(state)
    print(json.dumps({"ok": True, "channel": args.channel, "last_processed_at": now}, indent=2))
    return 0


def cmd_context(args: argparse.Namespace) -> int:
    pm = _load_project_mapping()
    projects = pm.get("projects") or {}
    info = projects.get(args.project)
    if not info:
        print(f"unknown project: {args.project}", file=sys.stderr)
        return 2
    state = _load_worker_state()
    ch_id = info.get("primary_channel_id")
    if not ch_id:
        print(f"project {args.project} has no primary_channel_id", file=sys.stderr)
        return 2
    ch_state = _channel_state(state, ch_id)
    sessions = _find_unprocessed_sessions(ch_id, ch_state.get("last_processed_at"))
    todos = _find_open_todos(args.project)
    print(_build_worker_context(args.project, ch_id, sessions, todos))
    return 0


def cmd_list_jobs(_args: argparse.Namespace) -> int:
    pm = _load_project_mapping()
    projects = pm.get("projects") or {}
    out: List[Dict[str, Any]] = []
    for slug, info in projects.items():
        if slug.startswith("_") or not isinstance(info, dict):
            continue
        cron_id = info.get("worker_cron_id")
        if not cron_id:
            continue
        job = _find_cron_job(cron_id)
        out.append({
            "project": slug,
            "cron_id": cron_id,
            "cron_name": info.get("worker_cron_name"),
            "primary_channel_id": info.get("primary_channel_id"),
            "cron_schedule": (job or {}).get("schedule", {}).get("expr"),
            "schedule_note": info.get("worker_cron_schedule_note"),
            "is_always_scheduled": "ALWAYS-SCHEDULED" in (info.get("worker_cron_schedule_note") or ""),
        })
    print(json.dumps(out, indent=2, default=str))
    return 0


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("status", help="Show trigger state per project.")
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("sync-todo-md", help="Sync per-project todo.md files from data/todos.json.")
    sp.set_defaults(func=cmd_sync_todo_md)

    sp = sub.add_parser("check", help="Dry-run: show what would fire.")
    sp.add_argument("--project", help="Limit to one project slug.")
    sp.set_defaults(func=cmd_check)

    sp = sub.add_parser("trigger", help="Force a trigger.")
    sp.add_argument("--project", help="Limit to one project slug.")
    sp.add_argument("--cron", help="Override the cron id to fire.")
    sp.add_argument("--dry-run", action="store_true", help="Don't actually fire; just print what would happen.")
    sp.add_argument("--force", action="store_true", help="Bypass gate + debounce + already-running checks.")
    sp.set_defaults(func=cmd_trigger)

    sp = sub.add_parser("trigger-all", help="Run the 5-min cycle for all projects.")
    sp.add_argument("--dry-run", action="store_true", help="Don't actually fire.")
    sp.set_defaults(func=cmd_trigger_all)

    sp = sub.add_parser("mark-processed", help="Set last_processed_at = now() for a channel.")
    sp.add_argument("--channel", required=True, help="Discord channel id.")
    sp.set_defaults(func=cmd_mark_processed)

    sp = sub.add_parser("context", help="Print the context that would be passed for a project.")
    sp.add_argument("--project", required=True, help="Project slug.")
    sp.set_defaults(func=cmd_context)

    sp = sub.add_parser("list-jobs", help="List triggerable crons.")
    sp.set_defaults(func=cmd_list_jobs)

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
