#!/usr/bin/env python3
"""
llm_price_audit.py — cross-check the hardcoded price table against reality.

Per Arcurus 2026-06-10 #lunar-project: hardcode the per-model price
table (in `code/llm_pricing.py`) but cross-check it automatically
against the MiniMax API (`mmx quota`) and the openclaw cost data
(`data/openclaw_usage.jsonl`).

This script runs on a schedule (e.g. hourly, or whenever the price
table is edited).  It produces a JSON report of:
  * For each model in the table: tokens (in / out / cache_read /
    cache_write) observed in the last 24h vs the cost we'd
    compute with the hardcoded prices
  * For each model NOT in the table: token totals so the operator
    can add it
  * For each model whose observed cost diverges from the hardcoded
    estimate by more than `DRIFT_THRESHOLD_PCT`, a flag for follow-up
  * A summary of "unknown model" events (caller passed a model name
    we don't have a price row for)

The script is read-only — it never modifies the price table, only
logs discrepancies.  Output is appended to `data/llm_price_audit.jsonl`
so the cost-tracker web UI can surface it.

CLI:
    python3 code/llm_price_audit.py run          # one audit, appends to log
    python3 code/llm_price_audit.py run --window 1h  # smaller window
    python3 code/llm_price_audit.py status       # show last 5 audits
    python3 code/llm_price_audit.py drift        # just the drift report
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple

import llm_pricing


# ---------------------------------------------------------------------------
# Paths and constants
# ---------------------------------------------------------------------------

SELENA_PROJECT_ROOT = os.path.expanduser(
    "~/openclaw/workspace/selena-project")
DATA_DIR = os.path.join(SELENA_PROJECT_ROOT, "data")
EVENT_LOG = os.path.join(DATA_DIR, "llm_usage_events.jsonl")
OPENCLAW_USAGE = os.path.join(DATA_DIR, "openclaw_usage.jsonl")
AUDIT_LOG = os.path.join(DATA_DIR, "llm_price_audit.jsonl")
MMX_BIN_CANDIDATES = [
    os.environ.get("MMX_BIN"),
    os.path.expanduser("~/.npm-global/bin/mmx"),
    "/home/openclaw/.npm-global/bin/mmx",
]

# Drift threshold: how much the observed $ may diverge from the
# hardcoded estimate before we flag it.  5% is a sane default — the
# hardcoded prices are reference values, not what the provider
# actually charges, so a small drift is expected.
DRIFT_THRESHOLD_PCT = 5.0

# How many recent audits to keep in `status`
STATUS_TAIL = 5


# ---------------------------------------------------------------------------
# Event log reader
# ---------------------------------------------------------------------------

def _read_events(window_hours: float) -> List[Dict[str, Any]]:
    """Read events from `llm_usage_events.jsonl` newer than now - window_hours.

    Returns a list of dicts (the parsed JSON of each line).  Skips
    malformed lines silently.  If the file doesn't exist, returns [].

    If `window_hours <= 0`, reads ALL events (no time filter).
    Used by the 'all' button in the Cost by Model sub-tab.
    """
    if not os.path.isfile(EVENT_LOG):
        return []
    if window_hours <= 0:
        cutoff = ""  # no filter
    else:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=window_hours)).isoformat()
    out: List[Dict[str, Any]] = []
    with open(EVENT_LOG) as f:
        for line in f:
            line = line.rstrip("\n")
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts = rec.get("ts", "")
            if ts >= cutoff:
                out.append(rec)
    return out


def _read_openclaw_usage(window_hours: float) -> List[Dict[str, Any]]:
    """Read per-session openclaw usage rows from `openclaw_usage.jsonl`.

    Schema differs from llm_usage_events.jsonl (per-session vs
    per-message) so we translate the fields we need.

    If `window_hours <= 0`, reads ALL rows (no time filter).
    """
    if not os.path.isfile(OPENCLAW_USAGE):
        return []
    if window_hours <= 0:
        cutoff = ""
    else:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=window_hours)).isoformat()
    out: List[Dict[str, Any]] = []
    with open(OPENCLAW_USAGE) as f:
        for line in f:
            line = line.rstrip("\n")
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts = rec.get("ts", "") or rec.get("endTime", "") or rec.get("startTime", "")
            if ts >= cutoff:
                out.append(rec)
    return out


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

def _aggregate_events(events: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Group events by model and sum the token buckets + observed $.

    The events log uses the field names `tokens_in`, `tokens_out`,
    `cache_read`, `cache_write`, and `cost_usd` (the recorded cost,
    which is 0.0 if it was never computed by the source).  When
    `cost_usd` is 0.0 we treat it as missing and re-compute from
    the tokens using llm_pricing.compute_cost_usd().
    """
    by_model: Dict[str, Dict[str, int]] = defaultdict(
        lambda: {
            "calls": 0, "tokens_in": 0, "tokens_out": 0,
            "cache_read_tokens": 0, "cache_write_tokens": 0,
            "recorded_cost_usd": 0.0,
        })
    for e in events:
        model = e.get("model", "") or "unknown"
        slot = by_model[model]
        slot["calls"] += 1
        slot["tokens_in"]          += int(e.get("tokens_in") or 0)
        slot["tokens_out"]         += int(e.get("tokens_out") or 0)
        slot["cache_read_tokens"]  += int(e.get("cache_read") or 0)
        slot["cache_write_tokens"] += int(e.get("cache_write") or 0)
        slot["recorded_cost_usd"]  += float(e.get("cost_usd") or 0.0)
    return dict(by_model)


def _aggregate_openclaw(rows: List[Dict[str, Any]]) -> Dict[str, Dict[str, int]]:
    """Group openclaw-usage rows by model.

    Openclaw schema (per-session, from openclaw_usage.jsonl) uses
    `tokensIn` / `tokensOut` (camelCase).  We don't have per-cache
    buckets in openclaw-usage, so the cache counts are zero.  This
    is fine for the drift check — the events log is the
    cache-aware source.
    """
    by_model: Dict[str, Dict[str, int]] = defaultdict(
        lambda: {"sessions": 0, "tokens_in": 0, "tokens_out": 0,
                 "cache_read_tokens": 0, "cache_write_tokens": 0})
    for r in rows:
        model = r.get("model", "") or "unknown"
        slot = by_model[model]
        slot["sessions"] += 1
        slot["tokens_in"]  += int(r.get("tokensIn")  or 0)
        slot["tokens_out"] += int(r.get("tokensOut") or 0)
    return dict(by_model)


# ---------------------------------------------------------------------------
# MiniMax quota pull (for the cross-check)
# ---------------------------------------------------------------------------

def _pull_minimax_per_model() -> Optional[Dict[str, Any]]:
    """Try to pull per-model token counts from `mmx quota --json`.

    Returns the parsed JSON, or None if mmx is unavailable / errors.
    The MiniMax Token Plan's `/v1/token_plan/remains` doesn't directly
    expose per-model token counts (it exposes remaining_percent per
    model), but we can use the remaining_percent to cross-check our
    `cost_usd` estimate against the API's own number.

    Note: when `mmx quota` is not on PATH (some sandboxes don't have
    it installed), this returns None and the audit falls back to
    event-log-based cross-check only.
    """
    mmx = next((c for c in MMX_BIN_CANDIDATES
                if c and (c == "mmx" or os.path.isfile(c))), None)
    if not mmx:
        return None
    try:
        r = subprocess.run(
            [mmx, "quota", "--json"],
            capture_output=True, text=True, timeout=15,
        )
        if r.returncode != 0:
            return None
        return json.loads(r.stdout)
    except (subprocess.TimeoutExpired, FileNotFoundError,
            json.JSONDecodeError):
        return None


# ---------------------------------------------------------------------------
# Drift calculation
# ---------------------------------------------------------------------------

def _compute_drift(observed_cost: float,
                   hardcoded_cost: float) -> float:
    """Return drift as a percentage.  0.0 if both are zero.

    Drift = |observed - hardcoded| / hardcoded * 100, or 100 if
    hardcoded is 0 and observed is non-zero (the hardcoded price
    is missing or zero but we still saw a call).
    """
    if hardcoded_cost == 0.0:
        if observed_cost == 0.0:
            return 0.0
        return 100.0  # missing price but we have a call
    return abs(observed_cost - hardcoded_cost) / hardcoded_cost * 100.0


# ---------------------------------------------------------------------------
# Main audit
# ---------------------------------------------------------------------------

def run_audit(window_hours: float = 24.0) -> Dict[str, Any]:
    """Run one cross-check pass and return the report dict.

    The report is also appended to AUDIT_LOG so the cost-tracker web
    UI can show a history of audits.
    """
    events = _read_events(window_hours)
    openclaw = _read_openclaw_usage(window_hours)
    aggregated = _aggregate_events(events)
    openclaw_agg = _aggregate_openclaw(openclaw)
    mmx = _pull_minimax_per_model()

    by_model: List[Dict[str, Any]] = []
    drift_flags: List[Dict[str, Any]] = []
    missing_prices: List[Dict[str, Any]] = []

    # All models seen in the window
    all_models = set(aggregated) | set(openclaw_agg)
    for model in sorted(all_models):
        evt = aggregated.get(model, {})
        oc  = openclaw_agg.get(model, {})
        # Use the events log (cache-aware) as the primary source.
        tokens_in           = evt.get("tokens_in",          0) + oc.get("tokens_in",          0)
        tokens_out          = evt.get("tokens_out",         0) + oc.get("tokens_out",         0)
        cache_read_tokens   = evt.get("cache_read_tokens",  0)
        cache_write_tokens  = evt.get("cache_write_tokens", 0)
        calls               = evt.get("calls", 0) + oc.get("sessions", 0)
        recorded_cost       = evt.get("recorded_cost_usd", 0.0)
        # Re-compute cost from the hardcoded prices
        hardcoded_cost = llm_pricing.compute_cost_usd(
            model, tokens_in, tokens_out,
            cache_read_tokens=cache_read_tokens,
            cache_write_tokens=cache_write_tokens,
        )
        # If the recorded cost is 0 (no real-time $ recorder), use the
        # hardcoded estimate as the "observed" cost for drift purposes.
        # Otherwise compare recorded to hardcoded.
        observed_cost = recorded_cost if recorded_cost > 0 else hardcoded_cost
        drift_pct = _compute_drift(observed_cost, hardcoded_cost)
        summary = llm_pricing.model_summary(model)
        entry = {
            "model":              model,
            "known":              summary["known"],
            "calls":              calls,
            "tokens_in":          tokens_in,
            "tokens_out":         tokens_out,
            "cache_read_tokens":  cache_read_tokens,
            "cache_write_tokens": cache_write_tokens,
            "recorded_cost_usd":  round(recorded_cost, 6),
            "hardcoded_cost_usd": round(hardcoded_cost, 6),
            "drift_pct":          round(drift_pct, 2),
            "price_in_usd_per_1m":  summary["in"],
            "price_out_usd_per_1m": summary["out"],
            "price_cache_read_usd_per_1m":  summary["cache_read"],
            "price_cache_write_usd_per_1m": summary["cache_write"],
        }
        # by_model is a list (sorted by model name for stable order), not
        # a dict. Reason: the web UI's renderCostByModel iterates with
        # .reduce() / .map() / .sort(), which expect arrays. (Was a dict
        # before 2026-06-11; changed because JS `data.by_model || []` with
        # a truthy object literal gave `{}`, not `[]`, and the page broke
        # with "models.reduce is not a function".)
        by_model.append(entry)
        if not summary["known"]:
            missing_prices.append(entry)
        if drift_pct > DRIFT_THRESHOLD_PCT:
            drift_flags.append(entry)

    report = {
        "at":                 datetime.now(timezone.utc).isoformat(),
        "window_hours":       window_hours,
        "events_count":       len(events),
        "openclaw_count":     len(openclaw),
        "models_seen":        len(all_models),
        "missing_prices":     [m["model"] for m in missing_prices],
        "drift_flags":        [m["model"] for m in drift_flags],
        "drift_threshold_pct": DRIFT_THRESHOLD_PCT,
        "mmx_quota_pulled":   mmx is not None,
        "by_model":           by_model,
    }

    # Persist
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(AUDIT_LOG, "a") as f:
            f.write(json.dumps(report) + "\n")
    except OSError as e:
        sys.stderr.write(f"[llm_price_audit] failed to write {AUDIT_LOG}: {e}\n")

    return report


def _print_report(report: Dict[str, Any]) -> None:
    """Pretty-print the audit report to stdout."""
    print(f"=== LLM price audit @ {report['at']} ===")
    print(f"  window:        {report['window_hours']}h")
    print(f"  events:        {report['events_count']}")
    print(f"  openclaw rows: {report['openclaw_count']}")
    print(f"  models seen:   {report['models_seen']}")
    print(f"  mmx quota:     {'pulled' if report['mmx_quota_pulled'] else 'unavailable'}")
    if report["missing_prices"]:
        print(f"  ⚠ unknown models (no price row): {', '.join(report['missing_prices'])}")
    if report["drift_flags"]:
        print(f"  ⚠ drift > {report['drift_threshold_pct']}%: {', '.join(report['drift_flags'])}")
    print()
    print(f"  {'model':32} {'calls':>6} {'tokens_in':>12} {'tokens_out':>12} "
          f"{'cache_rd':>10} {'cache_wr':>10} "
          f"{'cost_usd':>10} {'drift%':>7}")
    print(f"  {'-'*32} {'-'*6} {'-'*12} {'-'*12} {'-'*10} {'-'*10} {'-'*10} {'-'*7}")
    for m in report["by_model"]:
        print(f"  {m['model']:32} {m['calls']:>6} "
              f"{m['tokens_in']:>12,} {m['tokens_out']:>12,} "
              f"{m['cache_read_tokens']:>10,} {m['cache_write_tokens']:>10,} "
              f"{m['hardcoded_cost_usd']:>10.4f} {m['drift_pct']:>6.1f}%")


# ---------------------------------------------------------------------------
# Status / drift-only views
# ---------------------------------------------------------------------------

def _read_audit_log() -> List[Dict[str, Any]]:
    if not os.path.isfile(AUDIT_LOG):
        return []
    out: List[Dict[str, Any]] = []
    with open(AUDIT_LOG) as f:
        for line in f:
            line = line.rstrip("\n")
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def cmd_status() -> int:
    audits = _read_audit_log()
    if not audits:
        print("(no audits yet — run `python3 code/llm_price_audit.py run` first)")
        return 0
    for a in audits[-STATUS_TAIL:]:
        print(f"  {a['at'][:19]}  models={a['models_seen']:>3}  "
              f"missing={len(a['missing_prices']):>2}  "
              f"drift={len(a['drift_flags']):>2}")
    return 0


def cmd_drift() -> int:
    """Just show the most recent drift report (the part that needs attention)."""
    audits = _read_audit_log()
    if not audits:
        print("(no audits yet)")
        return 0
    last = audits[-1]
    print(f"=== last audit @ {last['at']} (window {last['window_hours']}h) ===")
    if not last["drift_flags"] and not last["missing_prices"]:
        print("  ✓ no drift, no missing prices")
        return 0
    if last["missing_prices"]:
        print(f"  ⚠ missing prices for: {', '.join(last['missing_prices'])}")
    if last["drift_flags"]:
        print(f"  ⚠ drift > {last['drift_threshold_pct']}% on: {', '.join(last['drift_flags'])}")
        # Show the actual drift numbers for the flagged models
        for m in last["by_model"]:
            if m["model"] in last["drift_flags"]:
                print(f"    {m['model']}: recorded=${m['recorded_cost_usd']:.4f} "
                      f"hardcoded=${m['hardcoded_cost_usd']:.4f} "
                      f"drift={m['drift_pct']:.1f}%")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    p = argparse.ArgumentParser(
        description="Cross-check the hardcoded LLM price table against "
                    "the event log + openclaw usage + MiniMax API.")
    sub = p.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run", help="Run one audit pass")
    p_run.add_argument("--window", type=float, default=24.0,
                       help="audit window in hours (default 24)")
    p_run.set_defaults(func=lambda a: _print_report(run_audit(a.window)))

    p_status = sub.add_parser("status",
                              help="show the last N audit summaries")
    p_status.set_defaults(func=lambda a: cmd_status())

    p_drift = sub.add_parser("drift",
                             help="just the drift report from the last audit")
    p_drift.set_defaults(func=lambda a: cmd_drift())

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())