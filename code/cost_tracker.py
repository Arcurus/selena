#!/usr/bin/env python3
"""
Cost Tracker for Selena v2
==========================

Daily cost / usage report for #cost-tracker (Discord channel).

Builds on top of llm_call_tracker.py — this CLI is the human-friendly
wrapper that:
  1. Pulls the current LLM usage snapshot (5h window, per-provider, per-project)
  2. Pulls the daily local event log (calls per project today)
  3. Renders a Discord-Markdown summary
  4. (Optional) POSTs the summary to a configured Discord channel via the
     bot token, OR writes it to a file for the cron wrapper to send

CLI:
    python3 cost_tracker.py status           # JSON status blob (no formatting)
    python3 cost_tracker.py report [--date YYYY-MM-DD]   # Markdown report (stdout)
    python3 cost_tracker.py post  [--date YYYY-MM-DD]   # Build + send to Discord
    python3 cost_tracker.py send --channel ID [--file PATH]  # Send arbitrary file
    python3 cost_tracker.py weekly                       # 7-day rolling summary

Configuration (env vars, all optional):
    COST_TRACKER_CHANNEL_ID  — default Discord channel to post to
                                (default: 1511700519582695456 = #cost-tracker)
    COST_TRACKER_DRY_RUN     — if "1", never actually post to Discord
    COST_TRACKER_DAILY_BUDGET_EUR — legacy per-day € cap, NO LONGER USED
        (we're on subscriptions now: MiniMax Token Plan + xAI Grok; default 0)

Daily-report cadence: produced by the `daily-cost-report` cron job.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

# Allow `from llm_call_tracker import get_tracker` when run as a script
HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from llm_call_tracker import get_tracker  # noqa: E402

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DEFAULT_CHANNEL_ID = "1511700519582695456"  # #cost-tracker (Selena Astra)
DATA_DIR = os.path.join(os.path.expanduser("~/openclaw/workspace/selena-project"), "data")
EVENT_LOG = os.path.join(DATA_DIR, "llm_usage_events.jsonl")
SNAPSHOT = os.path.join(DATA_DIR, "llm_usage_snapshot.json")
LLM_CALLS_FILE = os.path.join(DATA_DIR, "llm_calls.json")

# Approx LLM pricing (USD per 1M tokens). These are ROUGH — the real
# authoritative source for MiniMax is the Token Plan's remaining_percent,
# which is in the snapshot. We use these only for the per-model spend
# estimate when token counts are available.
PRICE_PER_1M_USD = {
    # xAI Grok
    "grok-4.3":              {"in": 3.00, "out": 15.00},
    "grok-4.3-fast":         {"in": 0.20, "out": 0.50},
    # Anthropic
    "anthropic/claude-sonnet-4.5": {"in": 3.00, "out": 15.00},
    "anthropic/claude-opus-4.6":   {"in": 15.00, "out": 75.00},
    # OpenAI
    "openai/gpt-4o":         {"in": 2.50, "out": 10.00},
    "openai/gpt-4o-mini":    {"in": 0.15, "out": 0.60},
    # MiniMax
    "MiniMax-M3":            {"in": 0.50, "out": 1.00},
    "MiniMax-M2.5":          {"in": 0.30, "out": 0.60},
}

EUR_PER_USD = 0.92  # rough; just for the €/$ display
# Legacy per-day € cap — kept for backward compatibility, but we no longer
# budget in € since we're on subscriptions (MiniMax Token Plan, xAI Grok).
# Set to 0 so the "Daily budget" line is no longer advertised in reports.
DAILY_BUDGET_EUR = float(os.environ.get("COST_TRACKER_DAILY_BUDGET_EUR", "0"))


# ---------------------------------------------------------------------------
# Bot token (for sending to Discord)
# ---------------------------------------------------------------------------

def _get_bot_token() -> Optional[str]:
    """Read Discord bot token from OpenClaw config (no auth needed for CLI)."""
    cfg_path = os.path.expanduser("~/.openclaw/openclaw.json")
    try:
        with open(cfg_path) as f:
            cfg = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    # Walk the config looking for the discord token
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


# ---------------------------------------------------------------------------
# Data loaders
# ---------------------------------------------------------------------------

def load_snapshot() -> Dict[str, Any]:
    """Read the latest LLM usage snapshot (5h + per-provider + per-project)."""
    if not os.path.exists(SNAPSHOT):
        return {}
    try:
        with open(SNAPSHOT) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def load_events(since: Optional[datetime] = None) -> List[Dict[str, Any]]:
    """Read the JSONL event log, optionally filtered to events after `since`."""
    if not os.path.exists(EVENT_LOG):
        return []
    out: List[Dict[str, Any]] = []
    with open(EVENT_LOG) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts_raw = ev.get("ts")
            if not ts_raw:
                continue
            try:
                ev_dt = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
            except ValueError:
                continue
            if since and ev_dt < since:
                continue
            out.append(ev)
    return out


def legacy_cumulative() -> int:
    """Read the legacy llm_calls.json `count` field (total all-time)."""
    if not os.path.exists(LLM_CALLS_FILE):
        return 0
    try:
        with open(LLM_CALLS_FILE) as f:
            return int(json.load(f).get("count", 0))
    except (FileNotFoundError, json.JSONDecodeError, ValueError):
        return 0


# ---------------------------------------------------------------------------
# OpenClaw session-usage integration
# ---------------------------------------------------------------------------
#
# Reads data/openclaw_usage.jsonl (built by `code/openclaw_usage.py`
# backfill) and renders a per-day summary. The new tracker is the
# authoritative source for sessions that ran through the OpenClaw
# gateway (cron jobs, subagents, discord/telegram/direct messages)
# with proper input/output token split. The cost math is per-model
# using PRICE_PER_1M_USD (grok-4.3 $3/$15, MiniMax-M3 $0.50/$1, etc.).

def _build_openclaw_section(target_date: str) -> Optional[Dict[str, Any]]:
    """Build the '🛰️ OpenClaw Sessions' section for the daily report.
    Returns None if the data file is missing (so the section just
    doesn't appear), or a dict with heading+lines otherwise."""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    try:
        import openclaw_usage
    except Exception:
        return None
    event_log = os.path.join(DATA_DIR, "openclaw_usage.jsonl")
    if not os.path.exists(event_log):
        return None
    try:
        day_start = datetime.strptime(target_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        day_end = day_start + timedelta(days=1)
        events = list(openclaw_usage._iter_events())
        events = openclaw_usage._filter_events(events, since=day_start, until=day_end)
        if not events:
            return None
        s = openclaw_usage._build_stats(events)
    except Exception as e:  # noqa: BLE001
        return {
            "heading": "🛰️ OpenClaw Sessions",
            "lines": [f"  - _error reading data: {e}_"],
        }
    eur = s["estCostUsd"] * EUR_PER_USD
    lines: List[str] = [
        f"Sessions: **{s['events']}** (distinct: {s['distinct_sessions']})",
        f"Tokens in/out: **{s['tokensIn']:,}** / **{s['tokensOut']:,}** (cache reads: {s['cacheRead']:,})",
        f"Est. spend (token-priced): **${s['estCostUsd']:.4f}** (~€{eur:.4f})",
    ]
    if s["per_kind"]:
        kind_str = ", ".join(f"{k}: {n}" for k, n in list(s["per_kind"].items())[:6])
        lines.append(f"Kinds: {kind_str}")
    if s["per_model"]:
        # Show top 3 models
        model_str = ", ".join(f"`{m}`: {n}" for m, n in list(s["per_model"].items())[:3])
        lines.append(f"Models: {model_str}")
    if s["per_cron"]:
        # Show top 3 cron jobs (using short IDs to keep the report tight)
        cron_str = ", ".join(f"`{k[:8]}…`: {n}" for k, n in list(s["per_cron"].items())[:3])
        lines.append(f"Top cron: {cron_str}")
    lines.append("_Source: `python3 code/openclaw_usage.py report --date " + target_date + "`_")
    return {"heading": "🛰️ OpenClaw Sessions", "lines": lines}


# ---------------------------------------------------------------------------
# mmx CLI integration
# ---------------------------------------------------------------------------
#
# Per Arcurus 2026-06-04: "please use the cli we made to query minimax stats
# and instruct the cron who posted the call stats to use this cli ... the
# cron can then display what the minimax cli call returns and also display
# our count".
#
# The mmx CLI is the canonical way to query the MiniMax Token Plan API
# (Bearer auth, parses JSON for us, and is the path the provider
# officially supports for our token plan).  Our llm_call_tracker.py has
# the same logic re-implemented, but using the CLI is the source of
# truth and avoids drift if the API shape changes.
#
# If the mmx binary is missing or fails, we degrade gracefully and the
# report still works (the section is just marked as "unavailable").

MMX_BIN = os.environ.get("MMX_BIN") or shutil.which("mmx") or "mmx"
MMX_QUOTA_TIMEOUT_S = 15


def run_mmx_quota(timeout: int = MMX_QUOTA_TIMEOUT_S) -> Dict[str, Any]:
    """Run `mmx quota` and return the parsed JSON blob.

    Returns a dict with at least:
      - ok: bool
      - source: "mmx-cli" on success, "error" otherwise
      - raw: stdout (truncated) — included so the cron can display it
      - error / exit_code / stderr (on failure)
    """
    out: Dict[str, Any] = {"ok": False, "source": "mmx-cli"}
    mmx_path = MMX_BIN
    if not mmx_path or not os.path.isfile(mmx_path):
        # shutil.which() may return None in some shells; fall back to PATH lookup
        mmx_path = "mmx"
    try:
        proc = subprocess.run(
            [mmx_path, "quota"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        out["error"] = f"mmx CLI not found at {mmx_path!r}"
        out["stderr"] = ""
        return out
    except subprocess.TimeoutExpired:
        out["error"] = f"mmx quota timed out after {timeout}s"
        out["stderr"] = ""
        return out
    except Exception as e:
        out["error"] = f"mmx quota failed: {type(e).__name__}: {e}"
        out["stderr"] = ""
        return out

    out["exit_code"] = proc.returncode
    out["stderr"] = (proc.stderr or "")[:400]
    if proc.returncode != 0:
        out["error"] = f"mmx quota exited {proc.returncode}: {(proc.stderr or '').strip()[:200]}"
        return out
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        out["error"] = f"mmx quota returned non-JSON: {e}"
        out["raw"] = (proc.stdout or "")[:400]
        return out
    out["ok"] = True
    out["data"] = data
    out["raw"] = (proc.stdout or "")[:600]  # truncated for display
    return out


def _format_mmx_section(quota: Dict[str, Any]) -> List[str]:
    """Render the `mmx quota` output as report lines.

    The MiniMax response shape is:
      model_remains: [ {model_name, current_interval_total_count,
                       current_interval_usage_count, current_interval_status,
                       current_interval_remaining_percent, ...}, ... ]
    We extract a compact, human-readable form and always include the raw
    JSON tail so the cron can prove the CLI was actually called.
    """
    if not quota.get("ok"):
        err = quota.get("error", "unknown error")
        return [
            f"mmx CLI: ❌ **{err}**",
            f"_(falling back to llm_call_tracker for window numbers)_",
        ]
    models = (quota.get("data") or {}).get("model_remains") or []
    if not models:
        return ["mmx CLI: returned no `model_remains`"]
    lines: List[str] = []
    for m in models:
        name = m.get("model_name", "?")
        used = m.get("current_interval_usage_count", 0)
        total = m.get("current_interval_total_count", 0)
        rem = m.get("current_interval_remaining_percent")
        weekly_rem = m.get("current_weekly_remaining_percent")
        interval_status = m.get("current_interval_status")
        weekly_status = m.get("current_weekly_status")
        status_glyph = {
            1: "🟢",
            2: "🟡",
            3: "⚪",
        }.get(interval_status, "❔")
        rem_str = f"{rem:.0f}%" if rem is not None else "?"
        weekly_str = f"{weekly_rem:.0f}%" if weekly_rem is not None else "?"
        if total:
            line = (
                f"  - {status_glyph} **{name}**: "
                f"5h {used}/{total} ({rem_str} left) · "
                f"week {weekly_str} left"
            )
        else:
            # Token plan returns 0/0 when the window has reset and no
            # API-level counter is exposed; remaining_percent is the
            # authoritative number.
            line = (
                f"  - {status_glyph} **{name}**: "
                f"5h {rem_str} left · week {weekly_str} left"
            )
        lines.append(line)
    return lines


# ---------------------------------------------------------------------------
# Report builders
# ---------------------------------------------------------------------------

def _eur(usd: float) -> float:
    return round(usd * EUR_PER_USD, 4)


def build_daily_report(date: Optional[str] = None) -> Dict[str, Any]:
    """Build the data blob for the daily cost report.

    Returns a dict with `header` (str) and `sections` (list of (heading, lines))
    so callers can render Markdown OR JSON as they like.
    """
    snap = load_snapshot()
    tracker = get_tracker()
    status = tracker.status()

    target_date = date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    day_start = datetime.strptime(target_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    day_end = day_start + timedelta(days=1)
    events_today = [e for e in load_events() if day_start <= datetime.fromisoformat(e["ts"].replace("Z", "+00:00")) < day_end]

    # Aggregate today's events
    per_provider_today: Dict[str, int] = {}
    per_project_today: Dict[str, int] = {}
    per_model_today: Dict[str, int] = {}
    est_usd_today = 0.0
    for ev in events_today:
        prov = ev.get("provider", "?")
        proj = ev.get("project") or "(no-project)"
        model = ev.get("model", "?")
        per_provider_today[prov] = per_provider_today.get(prov, 0) + 1
        per_project_today[proj] = per_project_today.get(proj, 0) + 1
        per_model_today[model] = per_model_today.get(model, 0) + 1
        tin = ev.get("tokens_in") or 0
        tout = ev.get("tokens_out") or 0
        if tin or tout:
            price = PRICE_PER_1M_USD.get(model)
            if price:
                est_usd_today += (tin * price["in"] + tout * price["out"]) / 1_000_000

    # 5h window
    local = status.get("local", {})
    limits = status.get("limits", {})

    # Provider polling health
    polling = status.get("polling", {}) or snap.get("polling", {})
    provider_lines: List[str] = []
    for prov, meta in polling.items():
        state = meta.get("state", "?")
        err = meta.get("last_error") or ""
        if state == "healthy":
            line = f"  - **{prov}**: healthy"
        elif state == "auth_error":
            line = f"  - **{prov}**: ⚠️ auth_error — {err[:80]}"
        elif state == "backoff":
            line = f"  - **{prov}**: ⏳ backoff — {err[:80]}"
        elif state == "paused":
            line = f"  - **{prov}**: ⏸️ paused"
        else:
            line = f"  - **{prov}**: {state}"
        # Quota info if available
        quota = (status.get("providers", {}) or {}).get(prov, {})
        rem = quota.get("remaining_percent")
        if rem is not None:
            line += f" (remaining: {rem:.0f}%)"
        provider_lines.append(line)

    # Warnings
    warnings = status.get("warnings", []) or []

    # Compose report
    header = f"💰 **Daily Cost Report — {target_date}**"
    sections: List[Dict[str, Any]] = []

    # --- HEADLINE: MiniMax API % vs our local count, side by side ---
    # The MiniMax % comes from the official /v1/token_plan/remains API.
    # The local count comes from our own JSONL event log.  We surface
    # BOTH at the top so the reader can compare them in one glance.
    headline_lines: List[str] = []
    mi = status.get("minimax_interval") or {}
    if mi.get("ok"):
        rem = mi.get("models", {}).get("general", {}).get("remaining_percent")
        soonest_s = mi.get("soonest_reset_s") or 0
        h, rem_s = divmod(int(soonest_s), 3600)
        m, _ = divmod(rem_s, 60)
        if rem is not None:
            headline_lines.append(
                f"🎯 **MiniMax API (authoritative)**: **{rem:.0f}% remaining** "
                f"(resets in {h}h {m}m)"
            )
        api_5h_used = mi.get("grand_total_used")
        if api_5h_used is not None and api_5h_used > 0:
            headline_lines.append(
                f"   ↳ MiniMax reports **{api_5h_used:,} calls** used in current 5h window"
            )
    else:
        err = (mi.get("error") or "unknown")[:80]
        headline_lines.append(f"🎯 **MiniMax API**: ⚠️ unavailable — {err}")
    headline_lines.append(
        f"📊 **Our 5h local count**: **{local.get('calls_5h', 0):,} / 4,500** "
        f"(target) / 4,500 (hard)"
    )
    headline_lines.append(
        f"🕰 **All-time cumulative**: **{legacy_cumulative():,}** calls "
        f"(since April 2026)"
    )
    sections.append({
        "heading": "🎯 Authoritative (MiniMax API) vs Ours (local counter)",
        "lines": headline_lines,
    })

    sections.append({
        "heading": "📊 Today",
        "lines": [
            f"Calls: **{len(events_today)}**",
            f"Est. spend (token-priced only): **${est_usd_today:.4f}** (~€{_eur(est_usd_today):.4f})",
            f"Billing model: **subscription** (MiniMax Token Plan + xAI Grok)",
        ],
    })

    sections.append({
        "heading": "⏱️ 5h Window",
        "lines": [
            f"Calls: **{local.get('calls_5h', 0)} / {limits.get('target_per_5h', 4000)}** (target) / {limits.get('hard_per_5h', 4500)} (hard)",
            f"Last 1h: {local.get('calls_1h', 0)}",
            f"Last 24h: {local.get('calls_24h', 0)}",
            f"Projected 5h from current rate: {local.get('projected_5h_from_rate', 0)}",
        ],
    })

    if per_provider_today:
        lines = [f"  - **{p}**: {n}" for p, n in sorted(per_provider_today.items(), key=lambda x: -x[1])]
        sections.append({"heading": "🔌 Per-provider (today)", "lines": lines})

    if per_project_today:
        lines = [f"  - **{p}**: {n}" for p, n in sorted(per_project_today.items(), key=lambda x: -x[1])]
        sections.append({"heading": "📁 Per-project (today)", "lines": lines})

    if per_model_today:
        lines = [f"  - `{m}`: {n}" for m, n in sorted(per_model_today.items(), key=lambda x: -x[1])]
        # cap to top 10 to keep the report readable
        if len(lines) > 10:
            head = lines[:10]
            head.append(f"  - _…and {len(lines) - 10} more_")
            lines = head
        sections.append({"heading": "🧠 Per-model (today)", "lines": lines})

    if provider_lines:
        sections.append({"heading": "🏥 Provider health", "lines": provider_lines})

    # mmx CLI output (authoritative for MiniMax Token Plan).
    # Per Arcurus 2026-06-04: "do not duplicate either number; just post
    # what the CLI returns."  The MiniMax % and our local count both
    # already appear in the headline at the top, so this section shows
    # the CLI output verbatim (with the source/exit-code line) — we do
    # NOT re-append the local count here, and we do NOT add a duplicate
    # % line.  The headline handles the cross-reference; this section
    # is the raw CLI dump for auditing.
    mmx_quota = run_mmx_quota()
    mmx_lines = _format_mmx_section(mmx_quota)
    mmx_lines.insert(0, f"_Source: `mmx quota` (exit {mmx_quota.get('exit_code', '?')})_")
    sections.append({"heading": "🛰️ mmx CLI (MiniMax Token Plan)", "lines": mmx_lines})

    # OpenClaw session-usage section (added 2026-06-06 per Arcurus).
    # Reads data/openclaw_usage.jsonl (proper input/output split
    # parsed from per-session .jsonl transcripts) and renders a
    # summary block. Soft-fails if the file isn't there yet.
    oc_section = _build_openclaw_section(target_date)
    if oc_section:
        sections.append(oc_section)

    if warnings:
        sections.append({
            "heading": "⚠️ Warnings",
            "lines": [f"  - {w}" for w in warnings],
        })

    if not events_today:
        sections.append({
            "heading": "📭",
            "lines": ["No LLM events logged today yet."],
        })

    return {
        "header": header,
        "sections": sections,
        "data": {
            "date": target_date,
            "calls_today": len(events_today),
            "est_usd_today": est_usd_today,
            "calls_5h": local.get("calls_5h", 0),
            "per_provider_today": per_provider_today,
            "per_project_today": per_project_today,
            "per_model_today": per_model_today,
            "warnings": warnings,
        },
    }


def build_weekly_report() -> Dict[str, Any]:
    """Build a 7-day rolling summary."""
    week_ago = datetime.now(timezone.utc) - timedelta(days=7)
    events = load_events(since=week_ago)

    per_day: Dict[str, int] = {}
    per_provider: Dict[str, int] = {}
    per_project: Dict[str, int] = {}
    for ev in events:
        try:
            ev_dt = datetime.fromisoformat(ev["ts"].replace("Z", "+00:00"))
        except (KeyError, ValueError):
            continue
        day = ev_dt.strftime("%Y-%m-%d")
        per_day[day] = per_day.get(day, 0) + 1
        prov = ev.get("provider", "?")
        proj = ev.get("project") or "(no-project)"
        per_provider[prov] = per_provider.get(prov, 0) + 1
        per_project[proj] = per_project.get(proj, 0) + 1

    days = sorted(per_day.keys())
    total = sum(per_day.values())
    avg = total / max(1, len(days))
    header = f"📈 **7-day Cost Summary** ({days[0] if days else 'n/a'} → {days[-1] if days else 'n/a'})"

    sections = [
        {
            "heading": "📊 Totals",
            "lines": [
                f"Calls: **{total:,}** over {len(days)} day(s)",
                f"Avg/day: {avg:.1f}",
            ],
        },
    ]
    if days:
        lines = [f"  - `{d}`: {per_day[d]}" for d in days]
        sections.append({"heading": "📅 Per-day", "lines": lines})
    if per_provider:
        lines = [f"  - **{p}**: {n}" for p, n in sorted(per_provider.items(), key=lambda x: -x[1])]
        sections.append({"heading": "🔌 Per-provider (7d)", "lines": lines})
    if per_project:
        lines = [f"  - **{p}**: {n}" for p, n in sorted(per_project.items(), key=lambda x: -x[1])]
        sections.append({"heading": "📁 Per-project (7d)", "lines": lines})

    return {"header": header, "sections": sections, "data": {"days": days, "total": total}}


def render_markdown(report: Dict[str, Any]) -> str:
    """Render a report dict to Discord-Markdown."""
    out = [report["header"], ""]
    for sec in report["sections"]:
        out.append(f"### {sec['heading']}")
        out.extend(sec["lines"])
        out.append("")
    # Trim trailing blanks
    while out and not out[-1].strip():
        out.pop()
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Discord post
# ---------------------------------------------------------------------------

def _discord_post(channel_id: str, text: str) -> Dict[str, Any]:
    """POST a message to a Discord channel via the bot token.

    Returns the parsed JSON response on success, or {"error": ...}.
    """
    token = _get_bot_token()
    if not token:
        return {"error": "no_bot_token"}
    url = f"https://discord.com/api/v10/channels/{channel_id}/messages"
    body = json.dumps({"content": text[:2000]}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bot {token}",
            "Content-Type": "application/json",
            "User-Agent": "selena-cost-tracker/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        return {"error": f"http_{e.code}", "body": e.read().decode("utf-8", errors="ignore")[:300]}
    except urllib.error.URLError as e:
        return {"error": f"url_error: {e.reason}"}


def post_to_discord(channel_id: Optional[str] = None, date: Optional[str] = None,
                    weekly: bool = False) -> Dict[str, Any]:
    """Build the report and POST it to Discord.

    Honors COST_TRACKER_DRY_RUN env var.
    """
    cid = channel_id or os.environ.get("COST_TRACKER_CHANNEL_ID") or DEFAULT_CHANNEL_ID
    report = build_weekly_report() if weekly else build_daily_report(date)
    text = render_markdown(report)
    if os.environ.get("COST_TRACKER_DRY_RUN") == "1":
        return {"dry_run": True, "channel_id": cid, "text": text, "data": report["data"]}
    result = _discord_post(cid, text)
    return {"sent": True, "channel_id": cid, "discord": result, "data": report["data"]}


def send_file(channel_id: str, file_path: str) -> Dict[str, Any]:
    """POST the contents of a text file as a Discord message."""
    with open(file_path) as f:
        text = f.read()
    result = _discord_post(channel_id, text[:2000])
    return {"sent": True, "channel_id": channel_id, "file": file_path, "discord": result}


# ---------------------------------------------------------------------------
# Per-project usage breakdown
# ---------------------------------------------------------------------------
#
# Per Arcurus 2026-06-04: "can you list usage per project and so on?"
# The llm_call_tracker maintains a per-(provider, model) deque of
# timestamped call records; from those we can build per-project rollups
# for whatever time window we want.  We expose two views:
#   - build_project_usage(window='today' | '5h' | '24h' | '7d')
#   - the `usage-by-project` CLI subcommand renders a Markdown table
#
# NOTE: the local deque is only populated by calls that go through the
# api_server's record endpoint, so the per-project numbers are a
# lower-bound on the true usage.  The mmx CLI section above is the
# authoritative number for the MiniMax Token Plan's 5h window.

PROJECT_USAGE_WINDOWS: Dict[str, Optional[float]] = {
    "5h":  5.0,
    "24h": 24.0,
    "7d":  24.0 * 7,
    "today": None,  # sentinel: local-midnight to now
}


def _known_provider_model_keys(tracker: Any) -> set:
    """Return the set of (provider, model) keys that have any recorded events.

    This is the only place we touch WindowedCounter's internals: the public
    API doesn't expose a way to enumerate keys, so we read them under the
    counter's lock.  Read-only.
    """
    keys: set = set()
    counter = tracker._counter  # noqa: SLF001
    with counter._lock:  # noqa: SLF001
        for (prov, model) in counter._events.keys():  # noqa: SLF001
            keys.add((prov, model))
    return keys


def build_project_usage(window: str = "today") -> Dict[str, Any]:
    """Build a per-project / per-model / per-provider rollup for `window`.

    Returns a dict with `header`, `lines` (Markdown), and `data` (raw).

    Implementation: iterates the public `WindowedCounter` API.  The
    tracker has `per_project_5h` / `per_provider_5h` for the 5h window
    specifically; for arbitrary windows we call `window(provider, model,
    hours)` per (provider, model) key and aggregate.
    """
    if window not in PROJECT_USAGE_WINDOWS:
        return {"error": f"unknown window: {window!r} (try: {list(PROJECT_USAGE_WINDOWS)})"}

    tracker = get_tracker()
    hours = PROJECT_USAGE_WINDOWS[window]

    if window == "5h":
        # Use the public, well-tested per_*_5h() methods.
        per_project = dict(tracker._counter.per_project_5h())  # noqa: SLF001
        per_provider = dict(tracker._counter.per_provider_5h())  # noqa: SLF001
        # For per-project × model we still need the full window() call.
        per_project_model: Dict[tuple, int] = {}
        per_project_provider: Dict[tuple, int] = {}
        total = sum(per_project.values())
        # Get the list of (provider, model) keys via a small in-memory scan
        # using the public `window()` method.
        for prov, model in _known_provider_model_keys(tracker):
            for e in tracker._counter.window(prov, model, hours=hours):
                proj = e.get("project") or "(no-project)"
                per_project_model[(proj, model)] = per_project_model.get((proj, model), 0) + 1
                per_project_provider[(proj, prov)] = per_project_provider.get((proj, prov), 0) + 1
        window_label = "last 5h"
    else:
        per_project = {}
        per_project_model = {}
        per_project_provider = {}
        per_provider = {}
        total = 0
        if hours is not None:
            # 24h / 7d windows
            for prov, model in _known_provider_model_keys(tracker):
                for e in tracker._counter.window(prov, model, hours=hours):
                    proj = e.get("project") or "(no-project)"
                    per_project[proj] = per_project.get(proj, 0) + 1
                    per_project_model[(proj, model)] = per_project_model.get((proj, model), 0) + 1
                    per_project_provider[(proj, prov)] = per_project_provider.get((proj, prov), 0) + 1
                    per_provider[prov] = per_provider.get(prov, 0) + 1
                    total += 1
            window_label = f"last {window}"
        else:
            # 'today' = local midnight to now
            now_local = datetime.now().astimezone()
            local_midnight = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
            if local_midnight.tzinfo is None:
                local_midnight = local_midnight.astimezone()
            cutoff = local_midnight.astimezone(timezone.utc)
            for prov, model in _known_provider_model_keys(tracker):
                for e in tracker._counter.window(prov, model, hours=24.0 * 2):
                    try:
                        t = datetime.fromisoformat(e["ts"])
                    except (TypeError, ValueError):
                        continue
                    if t.tzinfo is None:
                        t = t.replace(tzinfo=timezone.utc)
                    if t < cutoff:
                        continue
                    proj = e.get("project") or "(no-project)"
                    per_project[proj] = per_project.get(proj, 0) + 1
                    per_project_model[(proj, model)] = per_project_model.get((proj, model), 0) + 1
                    per_project_provider[(proj, prov)] = per_project_provider.get((proj, prov), 0) + 1
                    per_provider[prov] = per_provider.get(prov, 0) + 1
                    total += 1
            window_label = "today (local)"

    if total == 0:
        return {
            "header": f"📊 **Per-project usage — {window_label}**",
            "lines": [f"No LLM events recorded locally in {window_label}."],
            "data": {"window": window, "total": 0, "per_project": {}, "per_provider": {}},
        }

    lines: List[str] = []
    lines.append(f"Total events recorded locally: **{total}**")
    lines.append("")
    lines.append("**Per project:**")
    for proj, n in sorted(per_project.items(), key=lambda x: -x[1]):
        lines.append(f"  - **{proj}**: {n}")
    if per_provider:
        lines.append("")
        lines.append("**Per provider:**")
        for prov, n in sorted(per_provider.items(), key=lambda x: -x[1]):
            lines.append(f"  - **{prov}**: {n}")
    if per_project_model:
        lines.append("")
        lines.append("**Per project × model (top 15):**")
        items = sorted(per_project_model.items(), key=lambda x: -x[1])[:15]
        for (proj, model), n in items:
            lines.append(f"  - `{proj}` × `{model}`: {n}")

    return {
        "header": f"📊 **Per-project usage — {window_label}**",
        "lines": lines,
        "data": {
            "window": window,
            "window_label": window_label,
            "total": total,
            "per_project": per_project,
            "per_provider": per_provider,
            "per_project_model": {f"{k[0]}|{k[1]}": v for k, v in per_project_model.items()},
        },
    }


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------

def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(prog="cost_tracker", description="Daily cost / usage reporting for #cost-tracker")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_status = sub.add_parser("status", help="Print JSON status blob (no formatting)")
    p_status.add_argument("--weekly", action="store_true", help="7-day rolling summary")

    p_report = sub.add_parser("report", help="Print Markdown report to stdout")
    p_report.add_argument("--date", help="YYYY-MM-DD (default: today UTC)")
    p_report.add_argument("--weekly", action="store_true", help="7-day rolling summary")

    p_post = sub.add_parser("post", help="Build report and POST to Discord")
    p_post.add_argument("--channel", help="Channel ID (default: COST_TRACKER_CHANNEL_ID or #cost-tracker)")
    p_post.add_argument("--date", help="YYYY-MM-DD (default: today UTC)")
    p_post.add_argument("--weekly", action="store_true", help="7-day rolling summary")

    p_send = sub.add_parser("send", help="Send a text file's contents to a channel")
    p_send.add_argument("--channel", required=True, help="Channel ID")
    p_send.add_argument("--file", required=True, help="Path to text file")

    p_usage = sub.add_parser("usage-by-project", help="Per-project / per-model / per-provider rollup from local event log")
    p_usage.add_argument(
        "--window", default="today",
        help="Time window: today (local), 5h, 24h, 7d (default: today)",
    )
    p_usage.add_argument(
        "--format", choices=("md", "json"), default="md",
        help="Output format (default: md)",
    )

    p_mmx = sub.add_parser("mmx-quota", help="Run `mmx quota` and print parsed JSON (sanity check)")
    p_mmx.add_argument("--timeout", type=int, default=MMX_QUOTA_TIMEOUT_S, help="Timeout in seconds")

    args = parser.parse_args(argv[1:])

    if args.cmd == "status":
        if args.weekly:
            print(json.dumps(build_weekly_report(), indent=2, default=str))
        else:
            print(json.dumps(build_daily_report(), indent=2, default=str))
        return 0

    if args.cmd == "report":
        if args.weekly:
            report = build_weekly_report()
        else:
            report = build_daily_report(args.date)
        print(render_markdown(report))
        return 0

    if args.cmd == "post":
        result = post_to_discord(channel_id=args.channel, date=args.date, weekly=args.weekly)
        print(json.dumps(result, indent=2, default=str)[:3000])
        if result.get("error"):
            return 1
        return 0

    if args.cmd == "send":
        result = send_file(args.channel, args.file)
        print(json.dumps(result, indent=2, default=str)[:2000])
        if result.get("error"):
            return 1
        return 0

    if args.cmd == "usage-by-project":
        result = build_project_usage(args.window)
        if result.get("error"):
            print(f"error: {result['error']}", file=sys.stderr)
            return 2
        if args.format == "json":
            print(json.dumps(result, indent=2, default=str))
        else:
            print(result["header"])
            print()
            for line in result["lines"]:
                print(line)
        return 0

    if args.cmd == "mmx-quota":
        result = run_mmx_quota(timeout=args.timeout)
        print(json.dumps(result, indent=2, default=str)[:2000])
        return 0 if result.get("ok") else 1

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
