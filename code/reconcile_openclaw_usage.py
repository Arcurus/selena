#!/usr/bin/env python3
"""
Reconcile OpenClaw-direct LLM usage with the cost tracker
=========================================================

Background (per todo `f1a2b3c4-OPENCLAW-REC`, Arcurus 2026-06-04 audit):
  Cron agentTurn jobs run through the OpenClaw gateway
  (localhost:18789/v1/chat/completions). OpenClaw calls MiniMax, gets
  the response, and returns it to the agent. **No** call is recorded
  on our side unless we add it.

  Until this script exists, those calls are completely invisible to
  `cost_tracker.py` and `llm_call_tracker.py`:
    * no entry in `data/llm_usage_events.jsonl`
    * no entry in the 5h sliding-window counter
    * no contribution to per-project allocations

  This script reads `openclaw status --usage --json`, takes every
  session with non-null `totalTokens` that we have not yet recorded,
  and appends an event row to `data/llm_usage_events.jsonl` with
  `project = "selena-direct"`. The new `sessionId` field on each
  event row makes matching deterministic for future reconcilers.

  Forward compat: `openclaw status --usage` is a public, stable CLI
  subcommand. If OpenClaw renames or adds fields, we log a warning
  and skip the new fields — readers must tolerate missing keys.

CLI:
  python3 reconcile_openclaw_usage.py poll
      # Runs `openclaw status --usage --json`, processes all unseen
      # sessions, prints a one-line summary, appends events.

  python3 reconcile_openclaw_usage.py stats
      # Prints counts: seen sessions, sessions by project, etc.

  python3 reconcile_openclaw_usage.py reset-state
      # Wipes the seen-sessions state file (re-process everything).
      # DANGER: only for testing / after a schema migration.

  python3 reconcile_openclaw_usage.py peek [--limit N]
      # Reads stdin JSON (for ad-hoc inspection / debugging).

State file:  data/reconcile_openclaw_state.json
             { "seen": { "<sessionId>": { "ts": "...", "kind": "...",
                                          "model": "...",
                                          "totalTokens": N } } }
             Trims to last 5,000 sessions on every write.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

# Detect OpenRouter :free models so the cost tracker can distinguish
# zero-cost fallback calls from paid ones. Added 2026-06-09 (todo
# d6ecf2f0 — OpenRouter free-model investigation).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from free_models import is_free_model  # noqa: E402
except ImportError:  # pragma: no cover — graceful degradation
    def is_free_model(_model: Optional[str]) -> bool:  # type: ignore
        return False

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

SELENA_ROOT = os.path.expanduser("~/openclaw/workspace/selena-project")
DATA_DIR = os.path.join(SELENA_ROOT, "data")
EVENT_LOG_FILE = os.path.join(DATA_DIR, "llm_usage_events.jsonl")
STATE_FILE = os.path.join(DATA_DIR, "reconcile_openclaw_state.json")
RECONCILE_LOG = os.path.join(DATA_DIR, "reconcile_openclaw.log")

# Keep at most this many sessionIds in the state file. Older ones are
# dropped on each write so the file doesn't grow without bound.
MAX_SEEN = 5000

# Project label for synthesized events. Must match
# PROJECT_ALLOCATIONS["selena-direct"] in llm_call_tracker.py once
# that constant is added.
PROJECT_LABEL = "selena-direct"

# Provider label. We don't actually know which upstream provider the
# call hit from `openclaw status --usage` (only the model string), so
# we keep "minimax-portal" as a sensible default for the cron jobs
# (most of them go through MiniMax today). If the model string
# contains "grok" or another provider we use the matching name.
def _provider_for_model(model: Optional[str]) -> str:
    if not model:
        return "minimax-portal"
    m = model.lower()
    if "grok" in m or "xai" in m:
        return "xai"
    if "claude" in m or "openrouter" in m or "llama" in m or "qwen" in m:
        return "openrouter"
    # OpenAI: anything that starts with "gpt-", "o1", "o3", "o4",
    # "text-embedding-", or "openai/" is OpenAI. The cost tracker and
    # the web UI both use this string to bucket per-provider 5h call
    # counts, so it MUST be one of the names in
    # `llm_usage_snapshot.json` `providers.<key>`.
    openai_prefixes = (
        "gpt-", "o1", "o3", "o4", "text-embedding-", "openai/",
        "dall-e", "whisper-", "tts-",
    )
    if any(m.startswith(p) for p in openai_prefixes) or "openai" in m:
        return "openai"
    return "minimax-portal"


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
        # Keep the most-recent MAX_SEEN entries by `ts` field
        items = sorted(seen.items(),
                       key=lambda kv: kv[1].get("ts", ""),
                       reverse=True)[:MAX_SEEN]
        state["seen"] = dict(items)
    tmp = STATE_FILE + ".tmp"
    try:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        with open(tmp, "w") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, STATE_FILE)
    except OSError as e:
        # Don't crash the cron run because of state — just log.
        _log(f"warn: could not write state file: {e}")


def _log(msg: str) -> None:
    line = f"[{datetime.now(timezone.utc).isoformat()}] {msg}\n"
    try:
        with open(RECONCILE_LOG, "a") as f:
            f.write(line)
    except OSError:
        pass
    # Also print to stderr for the cron runner
    print(msg, file=sys.stderr)


# ---------------------------------------------------------------------------
# Event append (mirrors `append_event_log` in llm_call_tracker.py
# but adds the new `sessionId` field)
# ---------------------------------------------------------------------------

def _append_event(rec: Dict[str, Any]) -> None:
    """Append a single event to the JSONL log (crash-safe)."""
    try:
        os.makedirs(os.path.dirname(EVENT_LOG_FILE), exist_ok=True)
        with open(EVENT_LOG_FILE, "a") as f:
            f.write(json.dumps(rec) + "\n")
    except OSError as e:
        _log(f"warn: could not append event: {e}")


# ---------------------------------------------------------------------------
# Core: process a usage payload
# ---------------------------------------------------------------------------

def _extract_cron_job_id(key: Optional[str]) -> Optional[str]:
    """Pull the cron job UUID out of a session key like
    `agent:main:cron:ea0aa9e8-4e1f-4ead-9dfb-934cccc2f097`."""
    if not key:
        return None
    parts = key.split(":")
    if len(parts) >= 4 and parts[2] == "cron":
        return parts[3]
    return None


def _process_sessions(sessions: List[Dict[str, Any]],
                      state: Dict[str, Any],
                      dry_run: bool = False) -> Tuple[int, int, int]:
    """Process a list of session dicts. Returns
    (seen_count, new_count, skipped_no_tokens)."""
    seen = state.setdefault("seen", {})
    new_count = 0
    skipped_no_tokens = 0
    now_iso = datetime.now(timezone.utc).isoformat()

    for s in sessions:
        session_id = s.get("sessionId")
        total_tokens = s.get("totalTokens")
        if not session_id:
            # No sessionId = nothing to dedupe on, skip safely
            continue
        if total_tokens is None:
            skipped_no_tokens += 1
            continue
        if session_id in seen:
            continue
        model = s.get("model") or s.get("selectedModel") or "unknown"
        provider = _provider_for_model(model)
        kind = s.get("kind") or "unknown"
        key = s.get("key") or ""
        cron_job_id = _extract_cron_job_id(key)
        # OpenClaw exposes per-bucket token counts on each session:
        #   inputTokens  - raw input (non-cached) prompt tokens
        #   outputTokens - generated tokens
        #   cacheRead    - prompt tokens served from the provider's cache
        #   cacheWrite   - prompt tokens written to the provider's cache
        #   totalTokens  - SUM of the above (input + output + cacheRead + cacheWrite)
        # Earlier this function only read `totalTokens` and dumped it
        # into `tokens_in`, leaving `tokens_out`, `cache_read`, and
        # `cache_write` as None. That made the per-model Cost by Model
        # sub-tab show cache_read = 0 for all sessions, even though
        # real calls were hitting the cache (MiniMax-M3 alone had
        # ~1.6B cache_read tokens in 2026-06). Fixed 2026-06-12 per
        # Arcurus #cost-tracker: "in the Detailed table Cache read is
        # always 0 but for sure we had some cache reads, please fix."
        # Use per-bucket values when present, fall back to totalTokens
        # (degraded) when the per-bucket fields are missing.
        input_tokens  = s.get("inputTokens")
        output_tokens = s.get("outputTokens")
        cache_read    = s.get("cacheRead")
        cache_write   = s.get("cacheWrite")
        if input_tokens is None and output_tokens is None and \
                cache_read is None and cache_write is None:
            # Pre-2026-06-12 schema: only totalTokens available. Keep
            # the old behavior so old sessions still record something.
            input_tokens = total_tokens
        rec = {
            "ts": now_iso,
            "provider": provider,
            "model": model,
            "project": PROJECT_LABEL,
            "tokens_in":          input_tokens,
            "tokens_out":         output_tokens,
            "cache_read":         cache_read or 0,
            "cache_write":        cache_write or 0,
            "reasoning_tokens": None,
            "chars_in": None,
            "chars_out": None,
            "chars_reasoning": None,
            # New fields (2026-06-05): readers must tolerate missing.
            "sessionId": session_id,
            "source": "openclaw-usage-reconciler",
            "sessionKind": kind,
            "cron_job_id": cron_job_id,
            # New field (2026-06-09, todo d6ecf2f0): mark OpenRouter :free
            # calls so future reporting can split "free" from "paid".
            # Readers must tolerate missing — it's optional.
            "is_free": is_free_model(model),
        }
        if not dry_run:
            _append_event(rec)
        seen[session_id] = {
            "ts": now_iso,
            "kind": kind,
            "model": model,
            "totalTokens": total_tokens,
            "cron_job_id": cron_job_id,
        }
        new_count += 1

    return len(seen), new_count, skipped_no_tokens


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------

def cmd_poll(_args: argparse.Namespace) -> int:
    """Run `openclaw status --usage --json`, process, append, report."""
    try:
        proc = subprocess.run(
            ["openclaw", "status", "--usage", "--json"],
            capture_output=True, text=True, timeout=15,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        _log(f"error: failed to run openclaw: {e}")
        return 1
    if proc.returncode != 0:
        _log(f"error: openclaw exited {proc.returncode}: {proc.stderr.strip()[:200]}")
        return 1
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        _log(f"error: openclaw stdout is not JSON: {e}")
        return 1
    sessions = (
        payload.get("sessions", {}).get("recent", [])
        if isinstance(payload, dict) else []
    )
    if not isinstance(sessions, list):
        _log("warn: sessions.recent is not a list — skipping")
        return 0
    state = _load_state()
    seen_count, new_count, skipped = _process_sessions(sessions, state)
    _save_state(state)
    summary = (
        f"openclaw-usage-reconcile: sessions={len(sessions)} "
        f"new={new_count} skipped_no_tokens={skipped} "
        f"seen_total={seen_count} project={PROJECT_LABEL}"
    )
    _log(summary)
    # Print to stdout (cron captures this) — one-line summary
    print(summary)
    return 0


def cmd_stats(_args: argparse.Namespace) -> int:
    state = _load_state()
    seen = state.get("seen", {})
    by_kind: Dict[str, int] = {}
    by_model: Dict[str, int] = {}
    for sid, info in seen.items():
        k = info.get("kind", "unknown")
        m = info.get("model", "unknown")
        by_kind[k] = by_kind.get(k, 0) + 1
        by_model[m] = by_model.get(m, 0) + 1
    out = {
        "seen_total": len(seen),
        "by_kind": by_kind,
        "by_model": by_model,
        "state_file": STATE_FILE,
    }
    print(json.dumps(out, indent=2))
    return 0


def cmd_reset_state(_args: argparse.Namespace) -> int:
    if os.path.exists(STATE_FILE):
        os.remove(STATE_FILE)
    _log("warn: state file reset (seen sessions will be re-processed)")
    print("State file removed.")
    return 0


def cmd_peek(args: argparse.Namespace) -> int:
    """Read JSON from stdin (or a file) and pretty-print a digest."""
    src = sys.stdin.read()
    if args.input:
        with open(args.input) as f:
            src = f.read()
    if not src.strip():
        print("error: no JSON provided on stdin or --input", file=sys.stderr)
        return 1
    try:
        data = json.loads(src)
    except json.JSONDecodeError as e:
        print(f"error: not JSON: {e}", file=sys.stderr)
        return 1
    sessions = (
        data.get("sessions", {}).get("recent", [])
        if isinstance(data, dict) else []
    )
    limit = args.limit or 20
    print(f"sessions.recent[:{limit}]:")
    for s in sessions[:limit]:
        print(f"  - kind={s.get('kind')} agent={s.get('agentId')} "
              f"model={s.get('model')!r} "
              f"sid={(s.get('sessionId') or '?')[:8]}.. "
              f"tokens={s.get('totalTokens')} pUsed={s.get('percentUsed')}")
    return 0


# ---------------------------------------------------------------------------
# CLI entry
# ---------------------------------------------------------------------------

def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("poll", help="Poll openclaw status --usage and reconcile.")
    sp.set_defaults(func=cmd_poll)

    sp = sub.add_parser("stats", help="Show seen-session statistics.")
    sp.set_defaults(func=cmd_stats)

    sp = sub.add_parser("reset-state", help="Wipe the seen-sessions state file.")
    sp.set_defaults(func=cmd_reset_state)

    sp = sub.add_parser("peek", help="Read JSON from stdin/file and digest sessions.")
    sp.add_argument("--input", "-i", help="Read JSON from this file instead of stdin.")
    sp.add_argument("--limit", "-l", type=int, default=20,
                    help="Max sessions to print (default 20).")
    sp.set_defaults(func=cmd_peek)

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
