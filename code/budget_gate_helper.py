#!/usr/bin/env python3
"""
budget_gate_helper.py — universal 80% MiniMax budget gate for any agent.

Per Arcurus 2026-06-10 #lunar-project: *"every agent that runs autonomous
should have the same 80 budget reached goal for autonomous work pausing"*.

The MiniMax API is the source of truth.  `code/budget_gate.py` polls
`mmx quota` every 10 sec (was 5 min before 2026-06-11 per Arcurus
#lunar-project: "5 min for minimax budget update seems quite long,
better use 10 secs") via the `budget-gate.timer` and writes the
state to `data/budget_gate.json`:

  state="open"        → used_pct < 80   → all autonomous work OK
  state="closed-80"   → 80 ≤ used_pct < 95 → defer autonomous work
  state="closed-95"   → used_pct ≥ 95   → defer all (incl. 24h daily)

## Fail-close policy (added 2026-06-11 per Arcurus #lunar-project)

If the gate file is missing/stale/malformed/unknown, this helper used
to fail OPEN (allow work + stderr alarm). Per Arcurus: *"better use a
fail close policy and report an error (at most one time per day to the
important channel)"*. `gate_allows_new_work()` now returns
`allowed=False` in that case, and the helper posts ONE Discord message
to #selena-project-important per UTC day summarizing the failure. The
state file `data/budget_gate_helper_errors.json` dedupes by day.

## Usage from any agent (Python)

```python
from budget_gate_helper import gate_allows_new_work, gate_state, GateState

decision = gate_allows_new_work()
if decision.allowed:
    # do LLM work
    ...
else:
    # bail; log decision.reason + decision.used_pct
    return
```

For scripts that just want exit-code semantics:

```bash
# Exit 0 = proceed, exit 1 = defer
python3 code/budget_gate_helper.py check
```

For agents that want a one-line wall:

```python
from budget_gate_helper import check_or_exit
check_or_exit()  # raises SystemExit(1) on closed gate, returns None on open
```

This matches the Overseer's policy in
`selena-project-2/orchestrator/overseer.py:_read_budget_gate()`.
"""
from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

SELENA_PROJECT_ROOT = os.path.expanduser(
    "~/openclaw/workspace/selena-project")
DATA_DIR = os.path.join(SELENA_PROJECT_ROOT, "data")
BUDGET_GATE_FILE = os.path.join(DATA_DIR, "budget_gate.json")
BUDGET_GATE_MAX_AGE_S = 60  # 6x the 10-sec timer cadence (was 600 / "2x 5-min"; lowered 2026-06-11 after 08:13 CEST stale:1800s blip)


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------

class GateState:
    OPEN = "open"
    CLOSED_80 = "closed-80"
    CLOSED_95 = "closed-95"


@dataclass
class GateSnapshot:
    """Snapshot of the current budget-gate state.

    Attributes:
        state: "open" | "closed-80" | "closed-95" | None.
            None means "could not read" (file missing, stale, malformed,
            or unknown state value) — the helper fails CLOSE in that
            case (since 2026-06-11, per Arcurus #lunar-project).
        used_pct: 0-100, or None if unknown.
        remaining_pct: 100 - used_pct, or None.
        at: ISO timestamp of the gate file (when the budget-gate.timer
            last polled the MiniMax API).
        age_s: seconds since `at`, or None.
        stale: True if the file is older than BUDGET_GATE_MAX_AGE_S.
        error: human-readable error string (e.g. "stale:1200s > 600s"),
            or None.
        source: "file" if read from the JSON file, or "missing".
    """
    state: Optional[str]
    used_pct: Optional[float]
    remaining_pct: Optional[float]
    at: Optional[str]
    age_s: Optional[float]
    stale: bool
    error: Optional[str]
    source: str


@dataclass
class GateDecision:
    """Result of a gate check, suitable for a one-line if/else."""
    allowed: bool
    state: Optional[str]
    used_pct: Optional[float]
    reason: str  # human-readable; empty if allowed


# ---------------------------------------------------------------------------
# Reader
# ---------------------------------------------------------------------------

def read_gate() -> GateSnapshot:
    """Read the current budget-gate state from the JSON file.

    If the file is missing/stale/malformed/unknown, returns
    `state=None, error="..."`. The caller (`gate_allows_new_work`)
    now treats this as fail-CLOSE (defer all autonomous work) and
    posts one Discord message per UTC day to #selena-project-important
    summarizing the failure — added 2026-06-11 per Arcurus #lunar-project
    (was fail-open before 2026-06-11).
    """
    if not os.path.isfile(BUDGET_GATE_FILE):
        return GateSnapshot(
            state=None, used_pct=None, remaining_pct=None,
            at=None, age_s=None, stale=True,
            error="budget_gate_file_missing",
            source="missing",
        )
    try:
        with open(BUDGET_GATE_FILE) as f:
            gate = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        return GateSnapshot(
            state=None, used_pct=None, remaining_pct=None,
            at=None, age_s=None, stale=True,
            error=f"read_failed: {e}",
            source="missing",
        )
    state = gate.get("state")
    used_pct = gate.get("used_pct")
    at_str = gate.get("at")
    age_s = None
    stale = True
    if isinstance(used_pct, (int, float)):
        used_pct = float(used_pct)
    if at_str:
        try:
            at_dt = datetime.fromisoformat(at_str.replace("Z", "+00:00"))
            if at_dt.tzinfo is None:
                at_dt = at_dt.replace(tzinfo=timezone.utc)
            age_s = (datetime.now(timezone.utc) - at_dt).total_seconds()
            stale = age_s > BUDGET_GATE_MAX_AGE_S
        except (TypeError, ValueError):
            pass
    remaining_pct = (round(100.0 - used_pct, 2)
                     if isinstance(used_pct, (int, float)) else None)
    # Only honor the state if it's fresh AND one of the known values.
    # A stale or unknown state fails CLOSE (state=None); the caller
    # (gate_allows_new_work) then refuses to allow autonomous work
    # and posts one Discord error to #selena-project-important per
    # UTC day.  See the fail-CLOSE flip on 2026-06-11.
    effective_state: Optional[str] = None
    error: Optional[str] = None
    if not stale and state in (GateState.OPEN, GateState.CLOSED_80, GateState.CLOSED_95):
        effective_state = state
    else:
        if state not in (None, GateState.OPEN, GateState.CLOSED_80, GateState.CLOSED_95):
            error = f"unknown_state:{state}"
        elif stale:
            error = f"stale:{age_s:.0f}s > {BUDGET_GATE_MAX_AGE_S}s"
    return GateSnapshot(
        state=effective_state, used_pct=used_pct, remaining_pct=remaining_pct,
        at=at_str, age_s=age_s, stale=stale, error=error, source="file",
    )


# ---------------------------------------------------------------------------
# Decision policy
# ---------------------------------------------------------------------------

def gate_allows_new_work() -> GateDecision:
    """Return the decision for "should I run autonomous LLM work now?".

    Policy (per Arcurus 2026-06-10 #lunar-project, fail-close flip on
    2026-06-11 per Arcurus #lunar-project: "better use a fail close
    policy and report an error (at most one time per day to the
    important channel)"):
      - state="open"         → allowed=True
      - state="closed-80"    → allowed=False, defer
      - state="closed-95"    → allowed=False, defer (strictest)
      - state=None (any reason: missing, stale, malformed, unknown)
        → allowed=False (fail-CLOSE), with a stderr alarm AND a
        one-message-per-UTC-day Discord post to
        #selena-project-important summarizing the failure.  This
        replaced the pre-2026-06-11 fail-OPEN behavior; a flaky
        timer should NOT silently halt lunar work, so the helper
        now errs on the side of safety instead of availability.
    """
    snap = read_gate()
    if snap.state == GateState.OPEN:
        return GateDecision(
            allowed=True, state=snap.state, used_pct=snap.used_pct,
            reason="",
        )
    if snap.state in (GateState.CLOSED_80, GateState.CLOSED_95):
        return GateDecision(
            allowed=False, state=snap.state, used_pct=snap.used_pct,
            reason=(f"budget_gate={snap.state} used_pct={snap.used_pct} "
                    f"resets in {snap.age_s:.0f}s if applicable"),
        )
    # state is None: file missing/stale/malformed/unknown → fail CLOSE.
    # Per Arcurus 2026-06-11 #lunar-project the helper should
    # (a) refuse to allow autonomous work, AND
    # (b) post ONE Discord message per UTC day to
    #     #selena-project-important summarizing the failure.  The
    #     dedup is in `_post_gate_error_to_important()` so callers
    #     can call `gate_allows_new_work()` as often as they want
    #     without flooding the channel.
    sys.stderr.write(
        f"[budget_gate_helper] FAIL-CLOSE: {snap.error} (state={snap.state} "
        f"used_pct={snap.used_pct}); autonomous work DEFERRED — "
        f"check budget-gate.timer health\n"
    )
    try:
        _post_gate_error_to_important(snap)
    except Exception as _e:  # noqa: BLE001
        # Never let the reporter itself fail the gate check.
        sys.stderr.write(
            f"[budget_gate_helper] error-post failed: {_e}\n"
        )
    return GateDecision(
        allowed=False, state=None, used_pct=snap.used_pct,
        reason=f"gate_unavailable:{snap.error}",
    )


# ---------------------------------------------------------------------------
# Fail-close error reporter (added 2026-06-11 per Arcurus #lunar-project)
# ---------------------------------------------------------------------------
#
# Posts ONE message per UTC day to #selena-project-important
# (channel id 1495187458776891483) summarizing the gate failure.
# State file: data/budget_gate_helper_errors.json.  The function
# never raises — failure to post must not break the gate check.

# Channel id (set explicitly; same constant used by
# code/failure_reporter.py:DEFAULT_IMPORTANT_CHANNEL — kept in sync
# there too).
GATE_ERROR_CHANNEL_ID = "1495187458776891483"
GATE_ERROR_STATE_PATH = os.path.join(DATA_DIR, "budget_gate_helper_errors.json")


def _post_gate_error_to_important(snap: "GateSnapshot") -> None:
    """Post the gate failure to #selena-project-important at most
    once per UTC day.  Idempotent across multiple calls in the same
    day; resets at UTC midnight.

    Never raises.  Logs the outcome to stderr.  No-op if discord_client
    is not importable (eg. tests, missing token) — we'd rather
    silently miss the alert than crash the gate check.
    """
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    state: Dict[str, Any] = {}
    try:
        if os.path.isfile(GATE_ERROR_STATE_PATH):
            with open(GATE_ERROR_STATE_PATH) as f:
                state = json.load(f) or {}
    except (OSError, json.JSONDecodeError) as _e:
        # Corrupt state file — treat as empty and rewrite.
        sys.stderr.write(
            f"[budget_gate_helper] warn: {GATE_ERROR_STATE_PATH} "
            f"unreadable ({_e}); starting fresh\n"
        )
        state = {}

    last_posted_day = state.get("last_posted_day")
    last_error = state.get("last_error", "")
    if last_posted_day == today and last_error == snap.error:
        # Already posted today for this exact error; no-op.
        return

    # Try to import the notifier lazily so this module stays
    # importable in environments without a bot token (eg. unit tests).
    try:
        from discord_client import post_to_channel  # type: ignore
    except Exception as _e:  # noqa: BLE001
        sys.stderr.write(
            f"[budget_gate_helper] error-post skipped: discord_client "
            f"unavailable ({_e})\n"
        )
        return

    used = snap.used_pct
    used_str = f"{used:.1f}%" if isinstance(used, (int, float)) else "unknown"
    age = snap.age_s
    age_str = f"{age:.0f}s" if isinstance(age, (int, float)) else "n/a"
    at_str = snap.at or "n/a"
    text = (
        f"🚧 **MiniMax budget gate unavailable** — autonomous LLM "
        f"work paused (fail-close, per Arcurus 2026-06-11 #lunar-project)\n"
        f"• error: `{snap.error}`\n"
        f"• last known: state=`{snap.state}` used={used_str} at=`{at_str}` "
        f"age=`{age_str}`\n"
        f"• what to check: `systemctl --user status budget-gate.timer` "
        f"+ `journalctl --user -u budget-gate.service -n 30`\n"
        f"• this message posts at most once per UTC day; the next post "
        f"happens after midnight UTC or when the error string changes"
    )
    try:
        ok = post_to_channel(
            GATE_ERROR_CHANNEL_ID, text,
            project="selena-project",
            agent="budget-gate-helper",
            task="gate-unavailable",
        )
        if not ok:
            sys.stderr.write(
                f"[budget_gate_helper] error-post returned falsy; "
                f"not updating dedup state\n"
            )
            return
    except Exception as _e:  # noqa: BLE001
        sys.stderr.write(
            f"[budget_gate_helper] error-post failed: {_e}\n"
        )
        return

    # Mark posted so we don't spam the channel.
    state = {
        "last_posted_day": today,
        "last_posted_at": datetime.now(timezone.utc).isoformat(),
        "last_error": snap.error,
        "channel_id": GATE_ERROR_CHANNEL_ID,
    }
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = GATE_ERROR_STATE_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, GATE_ERROR_STATE_PATH)
    except OSError as _e:
        sys.stderr.write(
            f"[budget_gate_helper] warn: could not persist dedup state "
            f"({_e}); the next call today may re-post\n"
        )


def check_or_exit() -> None:
    """One-line guard for scripts: `check_or_exit()` at the top of
    any autonomous-work entry point.  Raises SystemExit(1) if the
    gate is closed (including the fail-close case where the gate
    file is missing/stale/malformed); returns None if allowed.

    Exit codes:
      0 = proceed
      1 = deferred (gate closed OR gate unavailable since 2026-06-11)
    """
    d = gate_allows_new_work()
    if d.allowed:
        return None
    sys.stderr.write(
        f"[budget_gate_helper] DEFER: {d.reason}\n"
    )
    raise SystemExit(1)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _cli() -> int:
    import argparse
    p = argparse.ArgumentParser(
        description="Universal 80%% MiniMax budget gate (per Arcurus "
                    "2026-06-10 #lunar-project).  Returns exit 0 if the "
                    "gate allows new work, exit 1 if it defers."
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    p_check = sub.add_parser(
        "check", help="exit 0 if work allowed, exit 1 if deferred")
    p_check.set_defaults(func=lambda a: _cmd_check())

    p_status = sub.add_parser(
        "status", help="print the current gate state as JSON")
    p_status.set_defaults(func=lambda a: _cmd_status())

    args = p.parse_args()
    return args.func(args)


def _cmd_check() -> int:
    d = gate_allows_new_work()
    if d.allowed:
        if d.state is None:
            print(f"ALLOW (gate unavailable: {d.reason})", file=sys.stderr)
        else:
            print(f"ALLOW (state={d.state} used_pct={d.used_pct})",
                  file=sys.stderr)
        return 0
    print(f"DEFER ({d.reason})", file=sys.stderr)
    return 1


def _cmd_status() -> int:
    snap = read_gate()
    print(json.dumps(snap.__dict__, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
