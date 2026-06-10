"""Compatibility shim — old name for app_llm_cost_tracker.

Per Arcurus 2026-06-07 #cost-tracker: renamed for clarity.

This shim now also provides a minimal get_tracker() compatibility layer
so that cost_tracker.py status (which expects tracker.status() and
tracker._counter) can run without crashing. The authoritative MiniMax
data comes from llm_usage_snapshot.json which is kept up-to-date by
the polling in api_server / budget_gate.

Why a `record()` and `sync_quotas()` live here too
-------------------------------------------------
The api_server's chat proxy (`/v1/chat/completions`) and the
`/api/llm-usage/record` endpoint both call `tracker.record(...)` and
`tracker.sync_quotas(...)` on the result of `get_tracker()`. Before
2026-06-08 these calls crashed with `AttributeError` and the
chat proxy silently swallowed the exception (the OpenClaw
direct-call cost line went unrecorded, and the daily report was
slightly undercounted). The shim now provides minimal implementations
so the calls don't crash.

Dedup story (important — read before changing):
  * `data/llm_usage_events.jsonl` is the single event log read by
    `cost_tracker.py` for the daily cost report. It is the SOURCE OF
    TRUTH for the report.
  * Two writers contribute to it today:
      1. `reconcile_openclaw_usage.py` (cron every 5 min) — reads
         `openclaw status --usage`, dedupes by `sessionId`, and
         appends one event per session.
      2. `_CompatTracker.record()` (this code, called from the
         api_server chat proxy) — only appends if the caller
         provides BOTH a `session_id` AND a `message_id`. The
         resulting event has `dedup_key = sessionId:messageId`,
         which matches the reconciler's key format, so the
         reconciler won't re-add it.
  * The public `/api/llm-usage/record` endpoint is for direct
    (non-OpenClaw) calls; it does NOT receive a session/message id
    and therefore only updates an in-memory counter (no file write).
    The 5h snapshot is driven by the reconciler + cron, not by
    this in-memory counter.
  * Result: no double-tracking. The reconciler dedupes by
    `sessionId:messageId`; the chat proxy uses the same key; the
    public record endpoint can't contribute file events.
"""
import warnings as _warnings
import json
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

_SNAPSHOT_PATH = os.path.expanduser(
    "~/openclaw/workspace/selena-project/data/llm_usage_snapshot.json"
)
_EVENT_LOG_PATH = os.path.expanduser(
    "~/openclaw/workspace/selena-project/data/llm_usage_events.jsonl"
)
# v2 events: per-session rows from the openclaw-usage-track.timer
# (5-min cron).  This is the live data source for the web UI's
# "Tokens per Project" and "Usage over time" charts because the
# per-message events in `_EVENT_LOG_PATH` only get written when the
# OpenClaw gateway sets the X-OpenClaw-Session-Id AND
# X-OpenClaw-Message-Id headers on the chat-completions request —
# in practice the gateway has stopped sending those headers, so
# `record()` is now in-memory only (inmem_only=True in the API
# log).  Per 2026-06-10 #cost-tracker, we now read BOTH files in
# `per_project_breakdown()` and `get_timeseries()` so the charts
# always have the most recent activity.  This is a fallback until
# either (a) the gateway starts sending the headers again or
# (b) the reconciler re-emits its rows into the events log.
_OPENCLAW_USAGE_LOG = os.path.expanduser(
    "~/openclaw/workspace/selena-project/data/openclaw_usage.jsonl"
)


def _existing_dedup_keys() -> set:
    """Return the set of dedup_keys already in llm_usage_events.jsonl.

    Used by `record()` to skip writes that would duplicate a session
    the reconciler has already logged. Cheap: O(file size) per call,
    but the file is bounded (~30k rows ≈ 15MB) and the widget polls
    it at most a few times per minute.
    """
    keys = set()
    try:
        with open(_EVENT_LOG_PATH) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                dk = rec.get("dedup_key")
                if dk:
                    keys.add(dk)
    except OSError:
        pass
    return keys


# ---------------------------------------------------------------------------
# Compatibility layer for cost_tracker.py
# cost_tracker.py calls:
#   tracker = get_tracker()
#   status = tracker.status()
#   ... and accesses tracker._counter for per_project_5h etc.
# We provide a thin wrapper that reads the authoritative snapshot.
# ---------------------------------------------------------------------------


class _CompatCounter:
    """Minimal stand-in for the old _counter object used by cost_tracker.py."""

    def __init__(self, snapshot: dict):
        self._snapshot = snapshot
        self._local = snapshot.get("local", {})

    def per_project_5h(self):
        return self._local.get("per_project_5h", {})

    def per_provider_5h(self):
        return self._local.get("per_provider_5h", {})

    def window(self, provider, model, hours=5.0):
        # Not implemented in the new snapshot; return empty list.
        # cost_tracker.py uses this for detailed per-model breakdowns.
        return []


class _CompatTracker:
    """Provides the .status() and ._counter interface expected by cost_tracker.py."""

    # In-memory counters for direct (non-OpenClaw) record() calls. These
    # are process-local; the api_server's /api/llm-usage/record endpoint
    # uses them so the 5h widget doesn't have to wait for the next
    # snapshot update to reflect a fresh direct call. They DO NOT
    # contribute to llm_usage_events.jsonl (that's the reconciler's job)
    # so they cannot double-count OpenClaw sessions.
    _inmem_lock_hint = "_inmem_counters"  # documentation only

    def __init__(self):
        self._snapshot = self._load_snapshot()
        self._counter = _CompatCounter(self._snapshot)
        # Per-provider rolling 5h window of direct (non-OpenClaw) calls.
        # Tuple = (ts_epoch, count). Pruned on read.
        self._inmem_direct_calls: Dict[str, list] = {}
        # Per-project rolling 5h window of direct (non-OpenClaw) calls.
        # Added 2026-06-09 per Arcurus #cost-tracker: the OW Rust binary
        # (and any other service that calls /api/llm-usage/record without
        # session+message id) bumps this counter so the per-project
        # cost rollup is real-time instead of waiting for the next
        # daily reconciliation. Merged into per_project_5h via
        # `per_project_5h_inmem()` below; /api/llm-usage adds it on top
        # of the snapshot's per_project_5h.
        self._inmem_direct_calls_by_project: Dict[str, list] = {}

    def _prune_inmem(self, window_s: float = 5 * 3600) -> None:
        cutoff = time.time() - window_s
        for prov, calls in list(self._inmem_direct_calls.items()):
            self._inmem_direct_calls[prov] = [(t, c) for (t, c) in calls if t >= cutoff]
            if not self._inmem_direct_calls[prov]:
                self._inmem_direct_calls.pop(prov, None)
        for proj, calls in list(self._inmem_direct_calls_by_project.items()):
            self._inmem_direct_calls_by_project[proj] = [(t, c) for (t, c) in calls if t >= cutoff]
            if not self._inmem_direct_calls_by_project[proj]:
                self._inmem_direct_calls_by_project.pop(proj, None)

    def per_project_5h_inmem(self) -> Dict[str, int]:
        """Return the current in-memory per-project counts (5h window).

        Counts the number of `record()` calls in the last 5h that
        provided a `project` parameter. Mirrors `per_provider_5h` but
        for projects. The snapshot's per_project_5h (which is
        OpenClaw-session-driven) is the authoritative source; this
        is the in-memory overlay for direct / non-OpenClaw callers
        like the OW Rust binary.
        """
        self._prune_inmem()
        return {p: sum(c for _, c in calls) for p, calls in self._inmem_direct_calls_by_project.items()}

    def per_project_breakdown(self, days: int = 7) -> Dict[str, Any]:
        """Per-project breakdown backed by BOTH the per-message events
        log AND the per-session openclaw-usage log.

        Per Arcurus 2026-06-10 #cost-tracker: the original
        implementation only read `llm_usage_events.jsonl` (per-message
        events written by the chat proxy), but the OpenClaw gateway
        stopped sending the X-OpenClaw-Session-Id and X-OpenClaw-
        Message-Id headers around 2026-06-08, which means
        `tracker.record()` has been falling back to the
        in-memory-only path (`inmem_only=True`) and no events have
        been written to the events log since.  The
        openclaw-usage-track.timer (still running, every 5 min)
        DOES write to `data/openclaw_usage.jsonl` — per-session rows
        with `tokensIn`/`tokensOut`/etc.  We now read BOTH files
        and merge the schemas so the per-project / timeseries charts
        in the web UI show live data.

        Returns:
          {
            "days": N,
            "total": N_calls,
            "per_project": {slug: {calls, tokens_in, tokens_out, models: {model: N}, providers: {prov: N}, last_call: iso}},
            "per_project_rollup": {parent_slug: {calls, tokens_in, tokens_out}},
            "per_project_provider": {(slug, prov): N},
            "per_project_model": {(slug, model): N},
            "sources": {events: N, openclaw_usage: N},  # NEW: diagnostic
          }
        """
        import datetime
        from collections import defaultdict

        result: Dict[str, Any] = {
            "days": days,
            "total": 0,
            "per_project": {},
            "per_project_rollup": {},
            "per_project_provider": {},
            "per_project_model": {},
            "sources": {"events": 0, "openclaw_usage": 0},  # diagnostic
        }
        cutoff: Optional[float] = None
        if days and days > 0:
            cutoff = (datetime.datetime.now(datetime.timezone.utc)
                      - datetime.timedelta(days=days)).timestamp()
        per_project: Dict[str, Dict[str, Any]] = defaultdict(lambda: {
            "calls": 0, "tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0,
            "models": defaultdict(int),
            "providers": defaultdict(int),
            "last_call": None,
        })
        per_proj_prov: Dict[tuple, int] = defaultdict(int)
        per_proj_model: Dict[tuple, int] = defaultdict(int)
        total = 0
        n_events = 0
        n_openclaw = 0

        # Source 1: llm_usage_events.jsonl (per-message, written by
        # the chat proxy when session_id+message_id are present).
        if os.path.exists(_EVENT_LOG_PATH):
            with open(_EVENT_LOG_PATH, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        e = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if cutoff and e.get("ts"):
                        try:
                            when = datetime.datetime.fromisoformat(
                                e["ts"].replace("Z", "+00:00")
                            ).timestamp()
                            if when < cutoff:
                                continue
                        except ValueError:
                            pass
                    proj = e.get("project") or "uncategorized"
                    prov = e.get("provider") or "?"
                    model = e.get("model") or "?"
                    slot = per_project[proj]
                    slot["calls"] += 1
                    slot["tokens_in"] += int(e.get("tokens_in") or 0)
                    slot["tokens_out"] += int(e.get("tokens_out") or 0)
                    # cost_usd was added to events 2026-06-04 —
                    # events before that may have None. Default to 0.0.
                    try:
                        slot["cost_usd"] += float(e.get("cost_usd") or 0.0)
                    except (TypeError, ValueError):
                        pass
                    slot["models"][model] += 1
                    slot["providers"][prov] += 1
                    ts = e.get("ts")
                    if ts and (not slot["last_call"] or ts > slot["last_call"]):
                        slot["last_call"] = ts
                    per_proj_prov[(proj, prov)] += 1
                    per_proj_model[(proj, model)] += 1
                    total += 1
                    n_events += 1

        # Source 2: openclaw_usage.jsonl (per-session, written by
        # openclaw-usage-track.timer every 5 min). Schema differs:
        # uses camelCase (`tokensIn`/`tokensOut`/`kind`/`agentId`)
        # instead of snake_case, and `kind` is the "project" axis
        # (`cron`/`discord`/`direct`/etc.) rather than a project slug.
        # We translate the schema on the fly.
        if os.path.exists(_OPENCLAW_USAGE_LOG):
            with open(_OPENCLAW_USAGE_LOG, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        e = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if cutoff and e.get("ts"):
                        try:
                            when = datetime.datetime.fromisoformat(
                                e["ts"].replace("Z", "+00:00")
                            ).timestamp()
                            if when < cutoff:
                                continue
                        except ValueError:
                            pass
                    # Translate the schema. `kind`+`channel` map
                    # into a friendly project label, matching the
                    # openclaw_cost_tracker's _project_label_for_event.
                    kind = e.get("kind") or "unknown"
                    if kind == "cron":
                        proj = "selena-cron"
                    elif kind in ("discord", "telegram"):
                        ch = e.get("channel") or "?"
                        proj = f"{kind}-{(ch or '?')[:8]}"
                    elif kind == "direct":
                        proj = "selena-direct"
                    elif kind == "subagent":
                        proj = "selena-subagent"
                    else:
                        proj = f"openclaw-{kind}"
                    prov = e.get("provider") or "?"
                    model = e.get("model") or "?"
                    slot = per_project[proj]
                    slot["calls"] += 1
                    slot["tokens_in"] += int(e.get("tokensIn") or 0)
                    slot["tokens_out"] += int(e.get("tokensOut") or 0)
                    try:
                        slot["cost_usd"] += float(e.get("estCostUsd") or 0.0)
                    except (TypeError, ValueError):
                        pass
                    slot["models"][model] += 1
                    slot["providers"][prov] += 1
                    ts = e.get("ts")
                    if ts and (not slot["last_call"] or ts > slot["last_call"]):
                        slot["last_call"] = ts
                    per_proj_prov[(proj, prov)] += 1
                    per_proj_model[(proj, model)] += 1
                    total += 1
                    n_openclaw += 1

        # Defaultdict → dict (and nested defaultdicts → dicts) for JSON.
        for slug, slot in per_project.items():
            slot["models"] = dict(slot["models"])
            slot["providers"] = dict(slot["providers"])
            # Round cost_usd to 6 decimals so JSON is readable.
            slot["cost_usd"] = round(slot.get("cost_usd", 0.0) or 0.0, 6)
        result["total"] = total
        result["sources"] = {"events": n_events, "openclaw_usage": n_openclaw}
        result["per_project"] = dict(per_project)
        result["per_project_provider"] = {f"{k[0]}|{k[1]}": v for k, v in per_proj_prov.items()}
        result["per_project_model"] = {f"{k[0]}|{k[1]}": v for k, v in per_proj_model.items()}

        # Merge in-memory counts on top of the JSONL counts so direct
        # / non-OpenClaw callers (OW Rust binary, etc.) show up
        # immediately instead of waiting for the next daily
        # reconciliation to land them in the JSONL log.
        # (Added 2026-06-09 per Arcurus #cost-tracker.)
        inmem_proj = self.per_project_5h_inmem() or {}
        inmem_prov: Dict[tuple, int] = {}
        # In-memory is keyed by (provider, project) implicitly; we
        # only track per-project counts in inmem. We don't know the
        # provider split, so leave per-project-provider as-is.
        for slug, n in inmem_proj.items():
            slot = result["per_project"].setdefault(slug, {
                "calls": 0, "tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0,
                "models": {}, "providers": {}, "last_call": None,
            })
            slot["calls"] += n
            result["total"] += n
            if slot["last_call"] is None:
                from datetime import datetime, timezone
                slot["last_call"] = datetime.now(timezone.utc).isoformat()
        # Rollup: sum children → parents
        # Uses result["per_project"] (which now includes the in-memory
        # merge) so direct calls like the OW Rust binary's open-world-running
        # LLM calls roll up to open-world-selena. Added 2026-06-09.
        # We need the project_mapping.json to know the parent map; load it
        # from the canonical location. (Cheap; ~1ms.)
        try:
            import os as _os
            pm_path = _os.path.join(_os.path.dirname(_SNAPSHOT_PATH), "project_mapping.json")
            with open(pm_path, encoding="utf-8") as f:
                pm = json.load(f)
            project_defs = pm.get("projects", {}) or {}
            from collections import defaultdict
            rollup: Dict[str, Dict[str, Any]] = defaultdict(lambda: {"calls": 0, "tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0})
            for slug, slot in result["per_project"].items():
                parent = project_defs.get(slug, {}).get("parentProject")
                if parent:
                    r = rollup[parent]
                    r["calls"] += slot["calls"]
                    r["tokens_in"] += slot["tokens_in"]
                    r["tokens_out"] += slot["tokens_out"]
                    r["cost_usd"] += slot.get("cost_usd", 0.0) or 0.0
            # Round cost_usd to 6 decimals so JSON is readable.
            for r in rollup.values():
                r["cost_usd"] = round(r["cost_usd"], 6)
            result["per_project_rollup"] = dict(rollup)
        except (OSError, json.JSONDecodeError):
            result["per_project_rollup"] = {}
        return result

    def _load_snapshot(self) -> dict:
        if os.path.exists(_SNAPSHOT_PATH):
            try:
                with open(_SNAPSHOT_PATH, "r") as f:
                    return json.load(f)
            except Exception:
                pass
        return {"local": {}, "minimax_interval": {"ok": False}, "polling": {}, "providers": {}}

    def status(self) -> Dict[str, Any]:
        """Return a status dict shaped like the old tracker.status() output."""
        snap = self._snapshot
        mi = snap.get("minimax_interval", {})
        local = snap.get("local", {})
        polling = snap.get("polling", {})
        providers = snap.get("providers", {})

        # Build the structure cost_tracker.py expects for the headline
        minimax_interval = {
            "ok": mi.get("ok", False),
            "models": mi.get("models", {}),
            "grand_total_used": mi.get("grand_total_used", 0),
            "soonest_reset_s": mi.get("soonest_reset_s", 0),
            "error": None if mi.get("ok") else (mi.get("error") or "snapshot unavailable"),
        }

        return {
            "minimax_interval": minimax_interval,
            "local": local,
            "polling": polling,
            "providers": providers,
            "warnings": [],
            "limits": snap.get("limits", {}),
        }

    # ------------------------------------------------------------------
    # Recording (added 2026-06-08 per Arcurus)
    # ------------------------------------------------------------------
    # Before 2026-06-08, these methods were missing and the api_server's
    # chat proxy and /api/llm-usage/record endpoint crashed (silently in
    # the chat proxy via `except Exception: pass`; noisily in the
    # endpoint as a 500). The cost report undercounted direct calls by
    # exactly that route. Adding the methods makes the cost line visible
    # without double-counting OpenClaw sessions (see the module docstring
    # for the dedup contract).
    def record(
        self,
        provider: str,
        model: str,
        project: Optional[str] = None,
        tokens_in: Optional[int] = None,
        tokens_out: Optional[int] = None,
        reasoning_tokens: Optional[int] = None,
        chars_in: Optional[int] = None,
        chars_out: Optional[int] = None,
        chars_reasoning: Optional[int] = None,
        session_id: Optional[str] = None,
        message_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Record a single LLM call. Returns a small status dict.

        Two paths:
          * Caller provided BOTH `session_id` AND `message_id`
            (this is how the OpenClaw chat proxy identifies the call):
            append a real event to `llm_usage_events.jsonl` with
            `dedup_key = f"{session_id}:{message_id}"`. The reconciler
            uses the same key format, so it will not re-add this event.
          * Caller provided neither (direct app call, e.g. the public
            `/api/llm-usage/record` endpoint): bump the in-memory
            per-provider 5h counter only. This makes the call visible
            to the running process for the rest of the 5h window but
            does NOT touch the events file (so the daily report, which
            is driven by the events file, is unaffected by these
            ephemeral counts).
        """
        now_epoch = time.time()
        # Always bump the in-memory 5h counter (cheap, doesn't touch disk).
        # This way direct (non-OpenClaw) calls show up in /api/llm-usage
        # immediately, even before the next snapshot refresh.
        self._inmem_direct_calls.setdefault(provider, []).append((now_epoch, 1))
        # Per-project in-memory bump (2026-06-09 per Arcurus #cost-tracker).
        # Project-aware so the OW Rust binary's open-world-running LLM
        # calls show up in /api/llm-usage right away, even before the
        # daily reconciliation adds them to the snapshot. We store the
        # raw slug the caller passed (which may include the new child
        # slugs like 'open-world-running' or 'open-world-dev'); rollup
        # to the parent is done at /api/llm-usage and /api/llm-usage/per-project
        # time, not here.
        proj_slug = project or "uncategorized"
        self._inmem_direct_calls_by_project.setdefault(proj_slug, []).append((now_epoch, 1))
        # If we have a real session+message id, append to the events
        # file — with dedup against the existing keys.
        if session_id and message_id:
            dedup_key = f"{session_id}:{message_id}"
            existing = _existing_dedup_keys()
            if dedup_key in existing:
                return {"ok": True, "dedup_key": dedup_key, "duplicate": True}
            rec = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "sessionId": session_id,
                "messageId": message_id,
                "dedup_key": dedup_key,
                "source": "api_server_record",
                "provider": provider,
                "model": model,
                "project": project or "uncategorized",
                "tokens_in": tokens_in or 0,
                "tokens_out": tokens_out or 0,
                "thinking_tokens": reasoning_tokens or 0,
                "cache_read": chars_in or 0,    # legacy field name (was chars_in in some callers)
                "cache_write": chars_reasoning or 0,
                "cost_usd": 0.0,
            }
            try:
                os.makedirs(os.path.dirname(_EVENT_LOG_PATH), exist_ok=True)
                with open(_EVENT_LOG_PATH, "a") as f:
                    f.write(json.dumps(rec) + "\n")
                return {"ok": True, "dedup_key": dedup_key, "duplicate": False}
            except OSError as e:
                return {"ok": False, "error": f"append failed: {e}"}
        return {"ok": True, "inmem_only": True}

    def sync_quotas(self, force: bool = False) -> Dict[str, Any]:
        """Compatibility no-op.

        The real quota sync used to live here. It has moved to:
          * `scripts/sync_llm_usage.sh` (cron every 5 min) for the
            snapshot, and
          * `scripts/reconcile_openclaw_usage.sh` (cron every 5 min) for
            the per-event feed.
        Both are still invoked by systemd user timers; see
        `~/.config/systemd/user/llm-usage-sync.timer` and
        `openclaw-usage-reconcile.timer`.

        This stub exists so the api_server's `?sync=1` call sites
        (`/api/llm-usage`, `/api/llm-usage/sync`) don't crash. The
        sync is fire-and-forget from those endpoints' perspective;
        they don't need the result inline.
        """
        return {"ok": True, "noop": True, "force": bool(force)}

    # ------------------------------------------------------------------
    # Time-series + alert state (added 2026-06-08 per Arcurus)
    # ------------------------------------------------------------------
    # The api_server's /api/llm-usage/timeseries and
    # /api/llm-usage/alert-state endpoints call these. They were
    # missing from _CompatTracker (a pre-existing bug — the original
    # LLMCallTracker had them but the shim was never updated). Without
    # them the endpoints crashed with `Remote end closed connection`
    # on every call. The implementations below are simple but
    # functional: they bucket the events from the authoritative
    # llm_usage_events.jsonl into the requested window.
    def get_timeseries(self, hours: int = 24) -> Dict[str, Any]:
        """Bucket events into a per-minute time-series from BOTH
        llm_usage_events.jsonl AND openclaw_usage.jsonl.

        Per Arcurus 2026-06-10 #cost-tracker: the per-message events
        log hasn't been written to since the OpenClaw gateway
        stopped sending session+message headers (so the chat proxy's
        `record()` is now inmem-only).  The per-session
        `openclaw_usage.jsonl` is still current, so we read from
        both and sum into the same buckets.

        The web UI's LLM Usage tab uses this for the
        "Calls Over Time" chart. Returns a dict shaped like the
        original tracker.get_timeseries() so the renderer can stay
        unchanged.  One new key: `sources` reports how many events
        came from each file (diagnostic only).
        """
        try:
            hours = int(hours)
        except (TypeError, ValueError):
            hours = 24
        hours = max(1, min(hours, 168))  # cap to 1 week
        now = time.time()
        cutoff = now - hours * 3600
        # Bucket size: 1 minute for <= 24h, 5 minutes for <= 72h, 15 min for > 72h.
        if hours <= 24:
            bucket_s = 60
        elif hours <= 72:
            bucket_s = 300
        else:
            bucket_s = 900
        nbuckets = int((hours * 3600) // bucket_s) + 1
        buckets = [0] * nbuckets
        total = 0
        per_provider: Dict[str, int] = {}
        sources = {"events": 0, "openclaw_usage": 0}

        def _ingest(path: str) -> None:
            nonlocal total
            if not os.path.exists(path):
                return
            try:
                with open(path) as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            rec = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        ts_raw = rec.get("ts")
                        if not ts_raw:
                            continue
                        try:
                            ev_dt = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
                            ev_epoch = ev_dt.timestamp()
                        except (ValueError, AttributeError):
                            continue
                        if ev_epoch < cutoff or ev_epoch > now:
                            continue
                        idx = int((ev_epoch - cutoff) // bucket_s)
                        if 0 <= idx < nbuckets:
                            buckets[idx] += 1
                            total += 1
                            prov = rec.get("provider", "unknown")
                            per_provider[prov] = per_provider.get(prov, 0) + 1
            except OSError:
                pass

        # Source 1: per-message events log (chat proxy).
        n0 = total
        _ingest(_EVENT_LOG_PATH)
        sources["events"] = total - n0
        # Source 2: per-session openclaw-usage log (5-min timer).
        n0 = total
        _ingest(_OPENCLAW_USAGE_LOG)
        sources["openclaw_usage"] = total - n0

        return {
            "hours": hours,
            "bucket_seconds": bucket_s,
            "nbuckets": nbuckets,
            "cutoff_epoch": cutoff,
            "now_epoch": now,
            "total": total,
            "buckets": buckets,
            "per_provider": per_provider,
            "sources": sources,  # diagnostic
        }

    def alert_state(self) -> Dict[str, Any]:
        """Compute the budget-alert state from the snapshot.

        Returns a dict shaped like the old AlertManager's state
        (matches what `/api/llm-usage/alert-state` consumers expect):
          {
            "state": "ok" | "warning" | "critical",
            "used": int, "budget": int, "used_pct": float,
            "history": [...],  # recent transitions
          }
        """
        snap = self._snapshot
        local = snap.get("local", {}) or {}
        limits = snap.get("limits", {}) or {}
        used = int(local.get("calls_5h", 0) or 0)
        budget = int(limits.get("hard_per_5h", 4500) or 4500)
        used_pct = (used / budget * 100) if budget else 0
        if used_pct >= 100:
            state = "critical"
        elif used_pct >= 80:
            state = "warning"
        else:
            state = "ok"
        return {
            "state": state,
            "used": used,
            "budget": budget,
            "used_pct": round(used_pct, 2),
            "cooldown_s": 300,
            "history": [],  # we don't persist history in the shim
        }


def get_tracker():
    """Return the process-wide singleton tracker.

    IMPORTANT (changed 2026-06-09 per Arcurus #cost-tracker): every
    caller MUST receive the same instance, otherwise the per-provider
    and per-project in-memory counters reset on every call. Previously
    this returned a fresh `_CompatTracker()` each time, which made the
    OW Rust binary's record() bumps invisible to /api/llm-usage.
    """
    global _TRACKER_SINGLETON
    if _TRACKER_SINGLETON is None:
        _TRACKER_SINGLETON = _CompatTracker()
    return _TRACKER_SINGLETON

_TRACKER_SINGLETON = None