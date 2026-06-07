#!/usr/bin/env python3
"""
OpenClaw Usage Tracker for Selena v2
====================================

Records every OpenClaw session (cron / discord / direct / subagent / etc.)
with PROPER per-turn input/output/cacheRead/cacheWrite token split by
parsing the per-session `.jsonl` transcripts on disk. The CLI replaces
the lossy `reconcile_openclaw_usage.py` (which only used `totalTokens`).

Why this exists
---------------
  * `openclaw status --usage --json` returns `sessions.recent` (10 most
    recent) and the field `totalTokens` which is the *session total*
    (not input vs output). Pricing all of it as input understates cost.
  * The per-session `.jsonl` transcripts include a per-turn `usage`
    block on assistant messages: `{"input": N, "output": M, "cacheRead":
    N, "cacheWrite": N, "totalTokens": N, "cost": {...}}`. Summing
    these gives the real split.
  * Sessions get pruned from `sessions.json` once they age out of the
    recent window, but the `.jsonl` transcript file persists on disk
    until the cleanup job removes it. So backfill walks the file
    system, not `sessions.json`.

Storage
-------
  * `data/openclaw_usage.jsonl`        — one row per session, with
    proper input/output split. Schema:
        {ts, sessionId, kind, channel, cronJobId, agentId,
         model, provider, startedAt, updatedAt, runtimeMs,
         tokensIn, tokensOut, cacheRead, cacheWrite,
         estCostUsd, source}
  * `data/openclaw_usage_state.json`   — dedupe state. Maps
    `sessionId -> {ts, kind, model, updatedAt, fileMtime}`. Trims
    to last N entries on each write. Re-running the same window
    produces zero new rows.

CLI
---
    python3 openclaw_usage.py status
    python3 openclaw_usage.py backfill [--dry-run] [--limit N]
    python3 openclaw_usage.py sync
    python3 openclaw_usage.py report [--date YYYY-MM-DD]
    python3 openclaw_usage.py timeseries [--hours 24] [--dimension model|project|agent|kind]

HTTP / API
----------
    Imported by api_server.py for the
    `/api/openclaw-usage/{stats,timeseries,sessions}` endpoints.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, Iterable, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

SELENA_ROOT = os.path.expanduser("~/openclaw/workspace/selena-project")
DATA_DIR = os.path.join(SELENA_ROOT, "data")
EVENT_LOG = os.path.join(DATA_DIR, "openclaw_usage.jsonl")
STATE_FILE = os.path.join(DATA_DIR, "openclaw_usage_state.json")
TRACKER_LOG = os.path.join(DATA_DIR, "openclaw_usage.log")

# OpenClaw session storage. Two paths because we have two agents (main
# + default). Read BOTH for the backfill.
SESSIONS_JSON_PATHS = [
    os.path.expanduser("~/.openclaw/agents/main/sessions/sessions.json"),
    os.path.expanduser("~/.openclaw/agents/default/sessions/sessions.json"),
]
SESSIONS_DIR = os.path.expanduser("~/.openclaw/agents/main/sessions")

# Keep at most this many sessionIds in the state file. Older ones are
# dropped on each write so the file doesn't grow without bound.
MAX_SEEN = 10_000

# Per-model pricing (USD per 1M tokens). Mirrors cost_tracker.py.
# Cache reads are billed at a small fraction on most providers; we
# apply 0.10 for input and 1.25 for output. The cache read rate is
# set to 0.1x of input to be conservative.
PRICE_PER_1M_USD: Dict[str, Dict[str, float]] = {
    # xAI Grok
    "grok-4.3":              {"in": 3.00,  "out": 15.00, "cacheRead": 0.30},
    "grok-4.3-fast":         {"in": 0.20,  "out": 0.50,  "cacheRead": 0.02},
    "grok-4.1-fast":         {"in": 0.20,  "out": 0.50,  "cacheRead": 0.02},
    # Anthropic
    "anthropic/claude-sonnet-4.5": {"in": 3.00, "out": 15.00, "cacheRead": 0.30},
    "anthropic/claude-sonnet-4.6": {"in": 3.00, "out": 15.00, "cacheRead": 0.30},
    "anthropic/claude-opus-4.6":   {"in": 15.00, "out": 75.00, "cacheRead": 1.50},
    # OpenAI
    "openai/gpt-4o":         {"in": 2.50,  "out": 10.00, "cacheRead": 0.25},
    "openai/gpt-4o-mini":    {"in": 0.15,  "out": 0.60,  "cacheRead": 0.015},
    "openai/gpt-5.4":        {"in": 5.00,  "out": 20.00, "cacheRead": 0.50},
    "openai/gpt-5.2-codex":  {"in": 5.00,  "out": 20.00, "cacheRead": 0.50},
    # MiniMax
    "MiniMax-M3":            {"in": 0.50,  "out": 1.00,  "cacheRead": 0.05},
    "MiniMax-M2.7-highspeed":{"in": 0.30,  "out": 0.60,  "cacheRead": 0.03},
    "MiniMax-M2.7":          {"in": 0.30,  "out": 0.60,  "cacheRead": 0.03},
    "MiniMax-M2.5":          {"in": 0.30,  "out": 0.60,  "cacheRead": 0.03},
    "MiniMax-M2.5-highspeed":{"in": 0.30,  "out": 0.60,  "cacheRead": 0.03},
    "MiniMax-M2.5-Lightning":{"in": 0.30,  "out": 0.60,  "cacheRead": 0.03},
    "MiniMax-M2.1":          {"in": 0.30,  "out": 0.60,  "cacheRead": 0.03},
    # GLM
    "z-ai/glm-5.1":          {"in": 0.50,  "out": 1.00,  "cacheRead": 0.05},
    "glm-5":                 {"in": 0.50,  "out": 1.00,  "cacheRead": 0.05},
}

# Default pricing if we don't recognize the model — assume the MiniMax
# M2.7-highspeed rate as a conservative middle-of-the-road estimate.
DEFAULT_PRICE = {"in": 0.30, "out": 0.60, "cacheRead": 0.03}

EUR_PER_USD = 0.92

PROJECT_LABEL = "openclaw-direct"

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def _log(msg: str) -> None:
    line = f"[{datetime.now(timezone.utc).isoformat()}] {msg}\n"
    try:
        os.makedirs(os.path.dirname(TRACKER_LOG), exist_ok=True)
        with open(TRACKER_LOG, "a") as f:
            f.write(line)
    except OSError:
        pass
    # Also print to stderr (cron captures this)
    print(msg, file=sys.stderr)


# ---------------------------------------------------------------------------
# State I/O
# ---------------------------------------------------------------------------

def _load_state() -> Dict[str, Any]:
    if not os.path.exists(STATE_FILE):
        return {"seen": {}}
    try:
        with open(STATE_FILE) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {"seen": {}}
    if not isinstance(data, dict) or "seen" not in data:
        return {"seen": {}}
    if not isinstance(data["seen"], dict):
        data["seen"] = {}
    return data


def _save_state(state: Dict[str, Any]) -> None:
    seen = state.get("seen", {})
    if len(seen) > MAX_SEEN:
        items = sorted(
            seen.items(),
            key=lambda kv: kv[1].get("updatedAt") or kv[1].get("ts") or "",
            reverse=True,
        )[:MAX_SEEN]
        state["seen"] = dict(items)
    tmp = STATE_FILE + ".tmp"
    try:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        with open(tmp, "w") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, STATE_FILE)
    except OSError as e:
        _log(f"warn: could not write state file: {e}")


# ---------------------------------------------------------------------------
# Event append
# ---------------------------------------------------------------------------

def _append_event(rec: Dict[str, Any]) -> None:
    try:
        os.makedirs(os.path.dirname(EVENT_LOG), exist_ok=True)
        with open(EVENT_LOG, "a") as f:
            f.write(json.dumps(rec) + "\n")
    except OSError as e:
        _log(f"warn: could not append event: {e}")


# ---------------------------------------------------------------------------
# sessions.json loading
# ---------------------------------------------------------------------------

def _load_sessions_index() -> Dict[str, Dict[str, Any]]:
    """Build a {sessionId: session_dict} index from sessions.json files.

    Also returns a {sessionId: "key"} for resolving kind/cron_job_id.
    """
    out: Dict[str, Dict[str, Any]] = {}
    for path in SESSIONS_JSON_PATHS:
        if not os.path.exists(path):
            continue
        try:
            with open(path) as f:
                d = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(d, dict):
            continue
        for key, sess in d.items():
            if not isinstance(sess, dict):
                continue
            sid = sess.get("sessionId")
            if not sid:
                continue
            # Don't clobber: if we already have this session, skip
            if sid in out:
                continue
            sess = dict(sess)
            sess["_key"] = key
            out[sid] = sess
    return out


def _infer_from_key(key: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """Pull (kind, cron_job_id) from a session key like
    `agent:main:cron:ea0aa9e8-...` or
    `agent:main:discord:channel:1511700519582695456`."""
    if not key:
        return None, None
    parts = key.split(":")
    if len(parts) < 3:
        return None, None
    kind = parts[2]  # cron / discord / telegram / subagent / direct / main / openai / dreaming-narrative-...
    cron_job_id = None
    if kind == "cron" and len(parts) >= 4:
        cron_job_id = parts[3]
    return kind, cron_job_id


def _infer_kind_from_path(path: str) -> str:
    """For a session file path, infer kind from the key prefix in
    sessions.json. Returns 'unknown' if not found."""
    base = os.path.basename(path)
    sid = base.replace(".jsonl", "")
    # Caller will resolve via index
    return "unknown"


# ---------------------------------------------------------------------------
# Per-session .jsonl transcript parsing
# ---------------------------------------------------------------------------

def _parse_session_transcript(path: str) -> Optional[Dict[str, Any]]:
    """Parse a per-session `.jsonl` transcript. Returns a dict with:
        - sessionId, startedAt, updatedAt, runtimeMs
        - model, provider (from model_change event)
        - tokensIn, tokensOut, cacheRead, cacheWrite (sum across all
          assistant messages)
        - costUsd (sum of `usage.cost.total` if present, else computed
          from tokensIn/Out using the price table)
        - first_user_msg_ts, last_assistant_ts

    Returns None if the file is missing or has no session header.
    """
    if not os.path.exists(path):
        return None

    session_id: Optional[str] = None
    started_at: Optional[int] = None  # epoch ms
    model: Optional[str] = None
    provider: Optional[str] = None
    first_ts: Optional[int] = None
    last_assistant_ts: Optional[int] = None
    tokens_in = 0
    tokens_out = 0
    cache_read = 0
    cache_write = 0
    cost_total = 0.0
    cost_seen = False
    turn_count = 0
    runtime_ms = 0

    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(e, dict):
                    continue
                t = e.get("type")
                ts = e.get("timestamp")
                if isinstance(ts, (int, float)):
                    ts_ms = int(ts) if ts > 1_000_000_000_000 else int(ts * 1000)
                elif isinstance(ts, str):
                    # ISO 8601 string
                    try:
                        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                        ts_ms = int(dt.timestamp() * 1000)
                    except (ValueError, AttributeError):
                        ts_ms = None
                else:
                    ts_ms = None
                if ts_ms is not None:
                    if first_ts is None or ts_ms < first_ts:
                        first_ts = ts_ms
                if t == "session":
                    session_id = e.get("id") or session_id
                    cwd = e.get("cwd")
                    if session_id and not started_at:
                        started_at = ts_ms
                elif t == "model_change":
                    provider = e.get("provider") or provider
                    mid = e.get("modelId")
                    if mid and not model:
                        model = mid
                elif t == "message":
                    # Two observed shapes:
                    #   1) {"type":"message","message":{"role":..,"usage":{..}}}
                    #   2) {"type":"message","role":..,"usage":{..},"content":..}
                    msg = e.get("message") if isinstance(e.get("message"), dict) else e
                    role = msg.get("role")
                    usage = msg.get("usage") or {}
                    if isinstance(usage, dict) and usage:
                        try:
                            tokens_in += int(usage.get("input") or 0)
                        except (TypeError, ValueError):
                            pass
                        try:
                            tokens_out += int(usage.get("output") or 0)
                        except (TypeError, ValueError):
                            pass
                        try:
                            cache_read += int(usage.get("cacheRead") or 0)
                        except (TypeError, ValueError):
                            pass
                        try:
                            cache_write += int(usage.get("cacheWrite") or 0)
                        except (TypeError, ValueError):
                            pass
                        cost = usage.get("cost") or {}
                        if isinstance(cost, dict) and cost.get("total"):
                            try:
                                cost_total += float(cost["total"])
                                cost_seen = True
                            except (TypeError, ValueError):
                                pass
                        if role == "assistant" and ts_ms is not None:
                            last_assistant_ts = ts_ms
                            turn_count += 1
                    # Sometimes the usage fields are at top level
                    elif "inputTokens" in e or "outputTokens" in e:
                        try:
                            tokens_in += int(e.get("inputTokens") or 0)
                        except (TypeError, ValueError):
                            pass
                        try:
                            tokens_out += int(e.get("outputTokens") or 0)
                        except (TypeError, ValueError):
                            pass
                        if role == "assistant" and ts_ms is not None:
                            last_assistant_ts = ts_ms
                            turn_count += 1
    except OSError:
        return None

    if not session_id:
        return None

    # If we didn't see a model_change, try the filename heuristic
    if not model:
        # Fall back: parse from session id + sessions.json index (caller
        # handles this). Mark as unknown here.
        model = "unknown"
    if not provider:
        provider = "unknown"

    updated_at = last_assistant_ts or started_at
    if started_at and updated_at:
        runtime_ms = max(0, updated_at - started_at)

    # Compute cost from tokens if not already present in the transcripts
    if not cost_seen:
        price = PRICE_PER_1M_USD.get(model, DEFAULT_PRICE)
        cost_total = (
            tokens_in * price["in"] / 1_000_000
            + tokens_out * price["out"] / 1_000_000
            + cache_read * price["cacheRead"] / 1_000_000
        )

    return {
        "sessionId": session_id,
        "startedAt": started_at,
        "updatedAt": updated_at,
        "model": model,
        "provider": provider,
        "tokensIn": tokens_in,
        "tokensOut": tokens_out,
        "cacheRead": cache_read,
        "cacheWrite": cache_write,
        "runtimeMs": runtime_ms,
        "turnCount": turn_count,
        "costUsd": cost_total,
        "transcriptFile": path,
    }


def _provider_for_model(model: Optional[str]) -> str:
    if not model:
        return "unknown"
    m = model.lower()
    if "grok" in m or "xai" in m:
        return "xai"
    if "claude" in m or "llama" in m or "qwen" in m or "/" in m:
        return "openrouter"
    if "minimax" in m:
        return "minimax-portal"
    return "minimax-portal"


# ---------------------------------------------------------------------------
# Backfill
# ---------------------------------------------------------------------------

def _iter_session_files() -> Iterable[str]:
    """Yield per-session .jsonl files (excluding sessions.json and
    .trajectory.jsonl companions). Sorted by mtime descending (newest
    first) so the most recent sessions are processed first."""
    if not os.path.isdir(SESSIONS_DIR):
        return
    candidates = []
    for name in os.listdir(SESSIONS_DIR):
        if not name.endswith(".jsonl"):
            continue
        if name == "sessions.json":
            continue
        if name.endswith(".trajectory.jsonl"):
            continue
        # Skip partial files: session files are <uuid>.jsonl
        if not re.match(r"^[0-9a-f-]{36}\.jsonl$", name):
            continue
        candidates.append(os.path.join(SESSIONS_DIR, name))
    candidates.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    yield from candidates


def _parse_trajectory_metadata(path: str) -> Optional[Dict[str, Any]]:
    """Read the first few lines of a `.trajectory.jsonl` companion
    file to extract sessionKey, agentId, messageProvider, etc.
    Returns a dict or None. Used to recover kind/cronJobId for
    sessions that have been pruned from sessions.json."""
    if not os.path.exists(path):
        return None
    out: Dict[str, Any] = {}
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f):
                if i > 10:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(e, dict):
                    continue
                sk = e.get("sessionKey")
                if sk and "sessionKey" not in out:
                    out["sessionKey"] = sk
                data = e.get("data") or {}
                for k in ("trigger", "messageChannel", "messageProvider", "agentId"):
                    v = data.get(k)
                    if v is not None and k not in out:
                        out[k] = v
    except OSError:
        return None
    return out or None


def _process_one(
    file_path: str,
    sessions_index: Dict[str, Dict[str, Any]],
    state: Dict[str, Any],
    dry_run: bool = False,
    force: bool = False,
) -> Tuple[str, str]:
    """Process a single .jsonl file. Returns (sessionId, status) where
    status is one of: 'recorded', 'updated', 'skipped', 'error'."""
    parsed = _parse_session_transcript(file_path)
    if not parsed:
        return "", "error"
    sid = parsed["sessionId"]
    seen = state.setdefault("seen", {})

    # Cross-reference with sessions.json (fastest source of truth)
    sess = sessions_index.get(sid) or {}
    key = sess.get("_key")
    kind, cron_job_id = _infer_from_key(key)

    # Fallback: read the trajectory file for kind/cron info when the
    # session has been pruned from sessions.json.
    if not kind:
        traj_path = file_path.replace(".jsonl", ".trajectory.jsonl")
        traj = _parse_trajectory_metadata(traj_path) or {}
        sk = traj.get("sessionKey") or ""
        if sk:
            kind, cron_job_id = _infer_from_key(sk)

    if not kind:
        kind = "unknown"
    channel = sess.get("channel") or sess.get("lastChannel")
    # If sessions.json didn't tell us, the trajectory's messageChannel
    # is a good fallback (it's a Discord channel id for cron jobs).
    if not channel and not sess:
        traj_path = file_path.replace(".jsonl", ".trajectory.jsonl")
        traj = _parse_trajectory_metadata(traj_path) or {}
        channel = traj.get("messageChannel")
    agent_id = sess.get("agentId") or "main"
    # Prefer the session-stored model/provider if the transcript
    # didn't surface a model_change event
    model = parsed["model"] if parsed["model"] != "unknown" else (sess.get("model") or "unknown")
    provider = parsed["provider"] if parsed["provider"] != "unknown" else _provider_for_model(model)
    if provider == "unknown" and model != "unknown":
        provider = _provider_for_model(model)
    if model == "unknown":
        return sid, "skipped"

    # The transcript may have tokens=0 (e.g. never ran an LLM). Skip
    # but record presence so we don't re-parse.
    file_mtime_ms = int(os.path.getmtime(file_path) * 1000)
    updated_at = parsed["updatedAt"] or parsed["startedAt"] or 0
    prior = seen.get(sid)
    if prior and not force:
        # Skip if we already have a record for this session, unless
        # the session's `updatedAt` has advanced (new turn since last
        # ingest). fileMtime alone is unreliable (it can change from
        # filesystem metadata updates without the session actually
        # being touched), so we only re-record on real progression.
        # Also: if the prior record is missing the v3 fields
        # (isFallback / cacheHitRatio / configuredModel), the schema
        # was upgraded after the row was written, so re-emit to
        # upgrade it. Default to False so older state entries
        # (pre-v3) get re-recorded.
        prior_has_v3 = bool(prior.get("has_v3_fields", False))
        if prior.get("updatedAt", -1) >= updated_at and prior_has_v3:
            return sid, "skipped"

    # If prior was a "no-tokens" placeholder, allow update
    # Capture model-selection context from sessions.json (if present)
    # so we can attribute "primary" vs "fallback-N" and the gateway's
    # reason when one is recorded. These fields are null for sessions
    # that ran on the primary model.
    configured_model = sess.get("configuredModel") or None
    selected_model = sess.get("selectedModel") or None
    model_selection_reason = sess.get("modelSelectionReason") or None
    is_fallback = bool(
        configured_model and selected_model
        and configured_model != selected_model
    )

    # Cache hit ratio: cacheRead / (cacheRead + non-cached input).
    # Useful as a model-efficiency metric — a high hit ratio means
    # the system prompt + conversation context is being reused
    # instead of re-billed as input.
    cache_read = parsed["cacheRead"]
    tokens_in = parsed["tokensIn"]
    total_input = cache_read + tokens_in
    cache_hit_ratio = (cache_read / total_input) if total_input > 0 else 0.0

    rec = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "sessionId": sid,
        "kind": kind,
        "channel": channel,
        "cronJobId": cron_job_id,
        "agentId": agent_id,
        "model": model,
        "provider": provider,
        "configuredModel": configured_model,
        "selectedModel": selected_model,
        "modelSelectionReason": model_selection_reason,
        "isFallback": is_fallback,
        "startedAt": parsed["startedAt"],
        "updatedAt": updated_at,
        "runtimeMs": parsed["runtimeMs"],
        "tokensIn": tokens_in,
        "tokensOut": parsed["tokensOut"],
        "cacheRead": cache_read,
        "cacheWrite": parsed["cacheWrite"],
        "cacheHitRatio": round(cache_hit_ratio, 4),
        "turnCount": parsed["turnCount"],
        "estCostUsd": round(parsed["costUsd"], 6),
        "source": "openclaw-usage-tracker-v2",
    }
    status = "updated" if prior else "recorded"
    if not dry_run:
        _append_event(rec)
    seen[sid] = {
        "ts": rec["ts"],
        "kind": kind,
        "model": model,
        "updatedAt": updated_at,
        "fileMtime": file_mtime_ms,
        "has_v3_fields": True,
    }
    return sid, status


def cmd_backfill(args: argparse.Namespace) -> int:
    """Walk all per-session .jsonl files and ingest with proper token
    split. Dedupes by sessionId. Idempotent."""
    dry_run = bool(args.dry_run)
    limit = args.limit
    sessions_index = _load_sessions_index()
    _log(f"backfill start (dry_run={dry_run}, limit={limit})")
    state = _load_state()
    counts: Counter = Counter()
    sample_errors: List[str] = []
    for i, fp in enumerate(_iter_session_files()):
        if limit and i >= limit:
            break
        try:
            sid, status = _process_one(fp, sessions_index, state, dry_run=dry_run)
            counts[status] += 1
        except Exception as e:  # noqa: BLE001
            counts["error"] += 1
            if len(sample_errors) < 5:
                sample_errors.append(f"{os.path.basename(fp)}: {e}")
    _save_state(state)
    summary = (
        f"openclaw-usage backfill: scanned={sum(counts.values())} "
        f"recorded={counts['recorded']} updated={counts['updated']} "
        f"skipped={counts['skipped']} errors={counts['error']} "
        f"dry_run={dry_run}"
    )
    _log(summary)
    print(summary)
    if sample_errors:
        print("Sample errors:", file=sys.stderr)
        for e in sample_errors:
            print(f"  {e}", file=sys.stderr)
    return 0


# ---------------------------------------------------------------------------
# Sync (incremental)
# ---------------------------------------------------------------------------

def cmd_sync(_args: argparse.Namespace) -> int:
    """Incremental: re-scan recent .jsonl files; only emits events for
    sessions whose file mtime or updatedAt changed since the last
    sync (i.e. session still active, or got a new turn)."""
    state = _load_state()
    sessions_index = _load_sessions_index()
    counts: Counter = Counter()
    for fp in _iter_session_files():
        try:
            sid, status = _process_one(fp, sessions_index, state)
            counts[status] += 1
        except Exception as e:  # noqa: BLE001
            counts["error"] += 1
    _save_state(state)
    summary = (
        f"openclaw-usage sync: scanned={sum(counts.values())} "
        f"recorded={counts['recorded']} updated={counts['updated']} "
        f"skipped={counts['skipped']} errors={counts['error']}"
    )
    _log(summary)
    print(summary)
    return 0


# ---------------------------------------------------------------------------
# Status / Report
# ---------------------------------------------------------------------------

def _iter_events() -> Iterable[Dict[str, Any]]:
    if not os.path.exists(EVENT_LOG):
        return
    with open(EVENT_LOG, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def _filter_events(
    events: Iterable[Dict[str, Any]],
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
) -> List[Dict[str, Any]]:
    """Filter events by the SESSION time (`updatedAt`), not the
    record time (`ts`). Falls back to `ts` only when `updatedAt` is
    missing."""
    out: List[Dict[str, Any]] = []
    for e in events:
        # Prefer the session's actual time; fall back to record time
        ts_str = e.get("updatedAt") or e.get("ts")
        if not ts_str:
            continue
        # updatedAt is an int (epoch ms) or an ISO string
        if isinstance(ts_str, (int, float)):
            ts = datetime.fromtimestamp(ts_str / 1000.0, tz=timezone.utc)
        elif isinstance(ts_str, str):
            try:
                ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                continue
        else:
            continue
        if since and ts < since:
            continue
        if until and ts >= until:
            continue
        out.append(e)
    return out


def _build_stats(events: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Build a stats blob from filtered events. Includes per-model
    cache-hit ratios and a fallback breakdown so the daily report /
    web UI can flag sessions where the gateway chose a fallback model."""
    per_model: Counter = Counter()
    per_provider: Counter = Counter()
    per_kind: Counter = Counter()
    per_agent: Counter = Counter()
    per_cron: Counter = Counter()
    per_channel: Counter = Counter()
    per_selection_reason: Counter = Counter()
    fallback_count = 0
    primary_count = 0
    tokens_in = 0
    tokens_out = 0
    cache_read = 0
    cost_total = 0.0
    distinct_sessions: set = set()
    last_event_ts: Optional[str] = None
    # Per-model: aggregate cache hit metrics so we can show hit ratio
    # per model. Key: model name; value: {cacheRead, tokensIn, count}.
    per_model_cache: Dict[str, Dict[str, int]] = {}
    for e in events:
        model = e.get("model") or "unknown"
        per_model[model] += 1
        per_provider[e.get("provider") or "unknown"] += 1
        per_kind[e.get("kind") or "unknown"] += 1
        per_agent[e.get("agentId") or "unknown"] += 1
        cid = e.get("cronJobId")
        if cid:
            per_cron[cid] += 1
        ch = e.get("channel")
        if ch:
            per_channel[ch] += 1
        # Fallback attribution: isFallback boolean + reason text
        if e.get("isFallback"):
            fallback_count += 1
        else:
            primary_count += 1
        reason = e.get("modelSelectionReason") or ("(primary)" if not e.get("isFallback") else "(unspecified)")
        per_selection_reason[reason] += 1
        # Per-model cache aggregation
        cm = per_model_cache.setdefault(model, {"cacheRead": 0, "tokensIn": 0, "count": 0})
        try:
            cm["cacheRead"] += int(e.get("cacheRead") or 0)
        except (TypeError, ValueError):
            pass
        try:
            cm["tokensIn"] += int(e.get("tokensIn") or 0)
        except (TypeError, ValueError):
            pass
        cm["count"] += 1
        try:
            tokens_in += int(e.get("tokensIn") or 0)
        except (TypeError, ValueError):
            pass
        try:
            tokens_out += int(e.get("tokensOut") or 0)
        except (TypeError, ValueError):
            pass
        try:
            cache_read += int(e.get("cacheRead") or 0)
        except (TypeError, ValueError):
            pass
        try:
            cost_total += float(e.get("estCostUsd") or 0)
        except (TypeError, ValueError):
            pass
        sid = e.get("sessionId")
        if sid:
            distinct_sessions.add(sid)
        ts = e.get("ts")
        if ts and (not last_event_ts or ts > last_event_ts):
            last_event_ts = ts
    # Compute overall + per-model cache hit ratios
    total_input_for_ratio = tokens_in + cache_read
    overall_cache_hit_ratio = (
        cache_read / total_input_for_ratio if total_input_for_ratio > 0 else 0.0
    )
    per_model_hit_ratio = {}
    for m, d in per_model_cache.items():
        tot = d["tokensIn"] + d["cacheRead"]
        per_model_hit_ratio[m] = round(d["cacheRead"] / tot, 4) if tot > 0 else 0.0
    return {
        "events": len(events),
        "distinct_sessions": len(distinct_sessions),
        "tokensIn": tokens_in,
        "tokensOut": tokens_out,
        "cacheRead": cache_read,
        "cacheHitRatio": round(overall_cache_hit_ratio, 4),
        "estCostUsd": round(cost_total, 6),
        "per_model": dict(per_model.most_common()),
        "per_model_cache_hit_ratio": per_model_hit_ratio,
        "per_provider": dict(per_provider.most_common()),
        "per_kind": dict(per_kind.most_common()),
        "per_agent": dict(per_agent.most_common()),
        "per_cron": dict(per_cron.most_common(20)),
        "per_channel": dict(per_channel.most_common(20)),
        "per_selection_reason": dict(per_selection_reason.most_common()),
        "fallback_count": fallback_count,
        "primary_count": primary_count,
        "last_event_ts": last_event_ts,
    }


def cmd_status(_args: argparse.Namespace) -> int:
    """Print summary JSON for the last 24h and all-time."""
    now = datetime.now(timezone.utc)
    last_24h = _filter_events(_iter_events(), since=now - timedelta(hours=24))
    last_5h = _filter_events(_iter_events(), since=now - timedelta(hours=5))
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    today = _filter_events(_iter_events(), since=today_start)
    all_events = list(_iter_events())
    out = {
        "now": now.isoformat(),
        "today": _build_stats(today),
        "last_5h": _build_stats(last_5h),
        "last_24h": _build_stats(last_24h),
        "all_time": _build_stats(all_events),
        "log_file": EVENT_LOG,
        "state_file": STATE_FILE,
    }
    print(json.dumps(out, indent=2))
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    """Render a Discord-Markdown report for the given date (default today)."""
    target_date = args.date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    day_start = datetime.strptime(target_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    day_end = day_start + timedelta(days=1)
    events = _filter_events(_iter_events(), since=day_start, until=day_end)
    s = _build_stats(events)
    eur = s["estCostUsd"] * EUR_PER_USD
    out = [
        f"🛰️ **OpenClaw Sessions — {target_date}**",
        "",
        f"Sessions: **{s['events']}** (distinct: {s['distinct_sessions']})",
        f"Tokens in/out: **{s['tokensIn']:,}** / **{s['tokensOut']:,}** (cache reads: {s['cacheRead']:,})",
        f"Est. spend (token-priced): **${s['estCostUsd']:.4f}** (~€{eur:.4f})",
        "",
    ]
    if s["per_kind"]:
        out.append("### By kind")
        for k, n in s["per_kind"].items():
            out.append(f"  - **{k}**: {n}")
        out.append("")
    if s["per_model"]:
        out.append("### By model")
        for m, n in s["per_model"].items():
            out.append(f"  - `{m}`: {n}")
        out.append("")
    if s["per_provider"]:
        out.append("### By provider")
        for p, n in s["per_provider"].items():
            out.append(f"  - **{p}**: {n}")
        out.append("")
    if s["per_cron"]:
        out.append("### By cron job (top 10)")
        for cid, n in list(s["per_cron"].items())[:10]:
            short = cid[:8] if cid else "?"
            out.append(f"  - `{short}…`: {n}")
        out.append("")
    print("\n".join(out))
    return 0


# ---------------------------------------------------------------------------
# Time series
# ---------------------------------------------------------------------------

def _bucketize(
    events: List[Dict[str, Any]],
    hours: int,
    dimension: str = "model",
) -> Dict[str, Any]:
    """Bucket events into per-hour buckets. Dimension selects the
    breakdown: model | provider | kind | agent | cron."""
    now = datetime.now(timezone.utc)
    start = now - timedelta(hours=hours)
    # Determine bucket count: 1h -> 60-min buckets, else 1h buckets
    if hours <= 6:
        bucket_minutes = 5
    elif hours <= 48:
        bucket_minutes = 60
    else:
        bucket_minutes = 60 * 4  # 4h buckets for 7d
    n_buckets = max(1, int((hours * 60) / bucket_minutes))
    bucket_seconds = bucket_minutes * 60
    start_epoch = int(start.timestamp())
    # Align start to bucket boundary
    start_epoch -= start_epoch % bucket_seconds
    buckets: List[Dict[str, Any]] = []
    for i in range(n_buckets):
        b_start = start_epoch + i * bucket_seconds
        buckets.append({
            "ts": datetime.fromtimestamp(b_start, tz=timezone.utc).isoformat(),
            "epoch": b_start,
            "_total": 0,
        })
    # Determine which dimension keys to show
    bucket_by_idx: Dict[int, Dict[str, Any]] = {b["epoch"]: b for b in buckets}
    seen_keys: set = set()
    for e in events:
        # Prefer the session's actual time (updatedAt) over the record
        # time (ts) for bucketing — otherwise a single backfill run
        # makes every event land in the same current-hour bucket.
        ts_str = e.get("updatedAt") or e.get("ts")
        if ts_str is None:
            continue
        if isinstance(ts_str, (int, float)):
            epoch_ms = int(ts_str)
        elif isinstance(ts_str, str):
            try:
                dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                epoch_ms = int(dt.timestamp() * 1000)
            except (ValueError, AttributeError):
                continue
        else:
            continue
        epoch = epoch_ms // 1000
        b_epoch = epoch - (epoch % bucket_seconds)
        b = bucket_by_idx.get(b_epoch)
        if not b:
            continue
        if dimension == "model":
            k = e.get("model") or "unknown"
        elif dimension == "provider":
            k = e.get("provider") or "unknown"
        elif dimension == "kind":
            k = e.get("kind") or "unknown"
        elif dimension == "agent":
            k = e.get("agentId") or "unknown"
        elif dimension == "project":
            # Projects: derive from kind+channel. cron=selena-{jobName},
            # discord=discord-{channel-short}, etc.
            k = _project_label_for_event(e)
        elif dimension == "cron":
            k = e.get("cronJobId") or "no-cron"
            k = k[:8] + "…" if len(k) > 8 else k
        else:
            k = "unknown"
        b[k] = b.get(k, 0) + 1
        b["_total"] += 1
        seen_keys.add(k)
    return {
        "buckets": buckets,
        "dimension": dimension,
        "keys": sorted(seen_keys),
        "hours": hours,
        "bucket_minutes": bucket_minutes,
    }


def _project_label_for_event(e: Dict[str, Any]) -> str:
    """Map an event to a friendly project label."""
    kind = e.get("kind") or "unknown"
    if kind == "cron":
        return "selena-cron"
    if kind in ("discord", "telegram"):
        ch = e.get("channel") or "?"
        return f"{kind}-{ch[:8]}"
    if kind == "direct":
        return "selena-direct"
    if kind == "subagent":
        return "selena-subagent"
    return f"openclaw-{kind}"


def cmd_timeseries(args: argparse.Namespace) -> int:
    hours = int(args.hours or 24)
    dimension = args.dimension or "model"
    now = datetime.now(timezone.utc)
    start = now - timedelta(hours=hours)
    events = _filter_events(_iter_events(), since=start)
    out = _bucketize(events, hours, dimension=dimension)
    print(json.dumps(out, indent=2))
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("backfill", help="Walk all session .jsonl files and ingest.")
    sp.add_argument("--dry-run", action="store_true", help="Don't write events.")
    sp.add_argument("--limit", type=int, default=0, help="Max files to process (0 = no limit).")
    sp.set_defaults(func=cmd_backfill)

    sp = sub.add_parser("sync", help="Incremental re-scan.")
    sp.set_defaults(func=cmd_sync)

    sp = sub.add_parser("status", help="Summary JSON (today/5h/24h/all-time).")
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("report", help="Discord-Markdown report.")
    sp.add_argument("--date", help="YYYY-MM-DD (default today UTC).")
    sp.set_defaults(func=cmd_report)

    sp = sub.add_parser("timeseries", help="JSON time-series buckets.")
    sp.add_argument("--hours", type=int, default=24, help="Window in hours (1..168).")
    sp.add_argument(
        "--dimension",
        default="model",
        choices=["model", "provider", "kind", "agent", "project", "cron"],
        help="Breakdown dimension.",
    )
    sp.set_defaults(func=cmd_timeseries)

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
