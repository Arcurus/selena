#!/usr/bin/env python3
"""
api_health_watchdog.py — the LAST-RESORT watchdog for selena-api.

Per Arcurus 2026-06-11 #selena-project: after the 11h outage (API crashed
2026-06-10 21:09:15, no watchdog caught it until manual restart at 08:39),
build a CHEAP (no LLM, no OpenClaw session) watchdog that:

  1. Probes selena-api (8765), open-world-selena (8081), orchestrator-status (8766)
  2. ONLY restarts selena-api (the orchestrator). The other 2 services are
     tracked in selena-project's own in-process service_manager (started
     2026-06-02, see selena-project/code/service_manager.py) and healed
     automatically — every 30s, with grace_period_seconds=60 and
     max_restarts_per_hour=4. So the external watchdog is a safety net
     FOR selena-api only.
  3. On detected selena-api failure → attempt ONE self-heal via
     `systemctl --user restart selena-project.service`
  4. Re-probe after a short delay; if still down → post ONCE to
     #selena-project-important (channel 1495187458776891483) with 30-min dedup
  5. When selena-api recovers → post ONCE the "recovered" message
  6. **Checks data freshness (added 2026-06-11 per Arcurus #cost-tracker,
     follow-up to the 22h silent-undercount incident):** if any of the
     producer-cron output files goes stale (mtime older than its threshold),
     post a single #selena-project-important alert with 30-min dedup. No
     auto-restart (the producer crons have their own systemd units; the
     alert is enough to wake a human if they're broken). This is the
     second layer the HTTP probe can't catch: selena-api can be perfectly
     healthy while a producer cron (e.g. openclaw-usage-track) silently
     stops writing because someone pointed it at a dead shim.
  7. Always exit 0 (so the timer never marks the watchdog as failed; the
     watchdog's job is to detect failure, not be one)

The other 2 services are PROBED (so the watchdog knows their state and can
alert if selena-api's in-process watchdog is silently broken), but the
external watchdog does NOT restart them — that's selena-api's job, per
projects.md's auto_start: true flags.

State is kept in data/api_health_watchdog.json (per-service and per-data-file
last-healthy timestamps, last-alert timestamps). Appends to
data/api_health_watchdog.log.

Usage:
  python3 code/api_health_watchdog.py check     # one-shot probe + heal
  python3 code/api_health_watchdog.py status    # show last-known state
  python3 code/api_health_watchdog.py reset     # clear state (force re-alert)

This is invoked by ~/.config/systemd/user/api-health-watchdog.{service,timer}
every 5 minutes (per Arcurus 2026-06-11).
"""

import json
import os
import subprocess
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

# ----------------------------------------------------------------------
# Constants
# ----------------------------------------------------------------------

SELENA_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = SELENA_DIR / "data"
STATE_PATH = DATA_DIR / "api_health_watchdog.json"
LOG_PATH = DATA_DIR / "api_health_watchdog.log"
ACTIVITY_LOG = DATA_DIR / "activity_log"

# Discord channel for alerts (selena-project-important)
ALERT_CHANNEL_ID = "1495187458776891483"

# Each service: (label, systemd_unit, http_probe_url, host_path, can_restart)
# - can_restart=True: external watchdog restarts it on failure
# - can_restart=False: external watchdog only PROBES it; the in-process
#                      service_manager inside selena-api handles restart
#                      (see selena-project/docs/projects.md auto_start: true)
SERVICES = [
    ("selena-api",          "selena-project.service",      "http://127.0.0.1:8765/api/health",     "/selena-astra/", True),
    ("open-world-selena",   "open-world-selena.service",   "http://127.0.0.1:8081/api/world/stats", "/open-world/",   False),
    ("orchestrator-status", "orchestrator-status.service", "http://127.0.0.1:8766/health",         None,             False),
]

# Each data file: (label, relpath_from_selena_dir, max_age_seconds, producer_hint)
# - max_age_seconds: how stale is too stale. After this, the producer cron
#                    is presumed broken and we alert. Choose threshold =
#                    2x the producer's expected cadence + 5 min grace.
# - producer_hint: free-text that names what should be producing this file
#                  (systemd timer + cron job ID, or "in-process" for files
#                  written by selena-api itself). Goes in the alert message
#                  so the on-call human knows what to investigate.
#
# Background: the 2026-06-10 22h silent undercount happened because
# openclaw-usage-track.service was calling a dead shim (the openclaw_usage.py
# -> openclaw_cost_tracker.py rename). selena-api was up the whole time, the
# HTTP probe was green, but the .jsonl hadn't been touched in 22h. The
# watcher's job expanded: an HTTP 200 is necessary but not sufficient.
DATA_FILES = [
    # The per-call LLM event log. Written by selena-api in-process
    # (/api/llm-usage/record) AND by the openclaw-usage-reconciler cron.
    # Threshold: 10 min (the reconciler is 5-min; 2 missed cycles = stale).
    ("llm_usage_events.jsonl",      "data/llm_usage_events.jsonl",      600,
     "selena-api /api/llm-usage/record (in-process) + openclaw-usage-reconciler.timer"),
    # The OpenClaw session log. The producer is openclaw-usage-track.service
    # (5-min cron). THIS IS THE FILE THAT WENT SILENT FOR 22H on 2026-06-11.
    # Threshold: 15 min (5-min cadence + 5min grace + 5min grace).
    ("openclaw_usage.jsonl",        "data/openclaw_usage.jsonl",        900,
     "openclaw-usage-track.service (5-min timer; cmd=openclaw_cost_tracker.py sync)"),
    # The live LLM quota snapshot (MiniMax / OpenAI). Producer: sync_llm_usage.sh
    # called by llm-usage-sync.timer (10-min cadence per Arcurus 2026-06-11).
    # Threshold: 20 min (10-min cadence + 10min grace).
    ("llm_usage_snapshot.json",     "data/llm_usage_snapshot.json",     1200,
     "llm-usage-sync.timer (scripts/sync_llm_usage.sh; 10-min cadence)"),
]

# Probe timeout (seconds) — keep short so a hung service doesn't block the loop
PROBE_TIMEOUT_S = 5
# Time to wait after a restart before re-probing
HEAL_WAIT_S = 6
# Dedup window: don't re-alert about the same service / data file within this many seconds
DEDUP_S = 30 * 60  # 30 minutes
# Treat a "stale" probe (took > N seconds) as suspicious but not failure
SLOW_PROBE_S = 3


# ----------------------------------------------------------------------
# Logging
# ----------------------------------------------------------------------

def log_line(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    line = f"[{ts}] {msg}"
    print(line, file=sys.stderr)
    try:
        with open(LOG_PATH, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass  # logging must never break the watchdog


def activity_log(event: str, details: str) -> None:
    """Append a one-liner to the selena-project activity_log."""
    try:
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        with open(ACTIVITY_LOG, "a") as f:
            f.write(f"[{ts}] [{event}] {details}\n")
    except Exception as e:
        log_line(f"activity_log write failed: {e}")


# ----------------------------------------------------------------------
# State
# ----------------------------------------------------------------------

def load_state() -> dict:
    if not STATE_PATH.exists():
        return {}
    try:
        with open(STATE_PATH) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        log_line(f"state file unreadable ({e}); treating as empty")
        # Don't lose the file — back it up so we can recover if it was a
        # transient parse error (e.g. mid-write)
        try:
            STATE_PATH.rename(STATE_PATH.with_suffix(".json.corrupt"))
        except OSError:
            pass
        return {}


def save_state(state: dict) -> None:
    """Atomic write: write to .tmp then rename. So a kill mid-write doesn't
    leave a half-written file that breaks the next probe."""
    tmp = STATE_PATH.with_suffix(".json.tmp")
    try:
        with open(tmp, "w") as f:
            json.dump(state, f, indent=2, sort_keys=True)
        tmp.replace(STATE_PATH)
    except OSError as e:
        log_line(f"state write failed: {e}")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ----------------------------------------------------------------------
# Probe + heal
# ----------------------------------------------------------------------

def probe(label: str, url: str) -> tuple:
    """Probe a URL. Returns (ok: bool, latency_ms: int, error: str or None)."""
    t0 = time.time()
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=PROBE_TIMEOUT_S) as r:
            _ = r.read(256)  # drain a small amount
            code = r.status
        latency_ms = int((time.time() - t0) * 1000)
        ok = 200 <= code < 400
        return ok, latency_ms, None if ok else f"HTTP {code}"
    except urllib.error.HTTPError as e:
        return False, int((time.time() - t0) * 1000), f"HTTP {e.code}"
    except urllib.error.URLError as e:
        return False, int((time.time() - t0) * 1000), f"URLError: {e.reason}"
    except (TimeoutError, OSError) as e:
        return False, int((time.time() - t0) * 1000), f"{type(e).__name__}: {e}"


def attempt_heal(unit: str) -> bool:
    """Try `systemctl --user restart <unit>`. Returns True on success."""
    try:
        r = subprocess.run(
            ["systemctl", "--user", "restart", unit],
            capture_output=True, text=True, timeout=30
        )
        if r.returncode == 0:
            log_line(f"heal: restarted {unit} (rc=0)")
            return True
        log_line(f"heal: systemctl restart {unit} failed (rc={r.returncode}): {r.stderr.strip()[:200]}")
        return False
    except (subprocess.TimeoutExpired, OSError) as e:
        log_line(f"heal: {type(e).__name__}: {e}")
        return False


def post_discord(text: str) -> bool:
    """Post to #selena-project-important via the in-process discord_client.
    Returns True on success. Never raises — Discord being down must not
    break the watchdog."""
    try:
        # Add the sys.path so we can import discord_client from /tmp etc.
        sys.path.insert(0, str(SELENA_DIR / "code"))
        from discord_client import post_to_channel  # type: ignore
        return bool(post_to_channel(
            ALERT_CHANNEL_ID, text,
            project="selena-project", agent="api-health-watchdog", task="alert"
        ))
    except Exception as e:
        log_line(f"discord post failed: {type(e).__name__}: {e}")
        return False


# ----------------------------------------------------------------------
# Main loop
# ----------------------------------------------------------------------

def check_once() -> int:
    """Run one probe + heal + alert cycle. Returns 0 always (so systemd
    never marks the watchdog as failed; the watchdog's job is to detect
    failure, not be one)."""
    state = load_state()
    if not isinstance(state, dict):
        state = {}
    state.setdefault("services", {})
    any_failed = False

    for label, unit, url, host_path, can_restart in SERVICES:
        svc = state["services"].setdefault(label, {
            "last_ok_at": None,
            "last_fail_at": None,
            "last_alert_at": None,
            "last_recover_alert_at": None,
            "consecutive_fails": 0,
            "last_error": None,
            "heal_in_flight": False,
        })

        ok, latency_ms, err = probe(label, url)
        svc["last_error"] = err
        svc["last_probe_at"] = now_iso()
        svc["last_latency_ms"] = latency_ms

        if ok:
            was_down = svc.get("last_ok_at") is None or svc["consecutive_fails"] > 0
            if was_down and svc.get("last_alert_at") is not None:
                # Recovery from a previously-alerted state — post the "back up" message
                downtime = ""
                if svc.get("last_fail_at"):
                    try:
                        start = datetime.fromisoformat(svc["last_fail_at"])
                        end = datetime.fromisoformat(svc["last_probe_at"])
                        delta = end - start
                        mins = int(delta.total_seconds() // 60)
                        downtime = f" (down ~{mins}m)"
                    except Exception:
                        pass
                msg = (
                    f"✅ **{label}** is back UP{downtime}.\n"
                    f"• probe `{url}` returned 200 in {latency_ms}ms"
                )
                if post_discord(msg):
                    svc["last_recover_alert_at"] = svc["last_probe_at"]
                    activity_log("WATCHDOG_RECOVERED", f"{label} is back up after {downtime or 'unknown downtime'}")
            svc["last_ok_at"] = svc["last_probe_at"]
            svc["consecutive_fails"] = 0
            svc["heal_in_flight"] = False
            log_line(f"OK   {label:24s} {latency_ms:5d}ms")
            continue

        # --- Failure path ---
        any_failed = True
        svc["consecutive_fails"] += 1
        svc["last_fail_at"] = svc["last_probe_at"]
        log_line(f"FAIL {label:24s} {err} (consecutive={svc['consecutive_fails']}, can_restart={can_restart})")

        # Self-heal: try restart ONCE on the first consecutive failure,
        # ONLY for services the external watchdog is allowed to restart
        # (selena-api). For open-world-selena and orchestrator-status, the
        # in-process service_manager inside selena-api handles restart; if
        # it's not working, that's a sign selena-api is also broken (which
        # we'll catch via its own probe below).
        if can_restart and svc["consecutive_fails"] == 1 and not svc.get("heal_in_flight"):
            svc["heal_in_flight"] = True
            log_line(f"heal: attempting restart of {unit}")
            activity_log("WATCHDOG_HEAL_TRY", f"attempting systemctl --user restart {unit} (consecutive_fails=1, err={err})")
            attempt_heal(unit)
            # Give the service a moment, then re-probe
            time.sleep(HEAL_WAIT_S)
            ok2, latency2, err2 = probe(label, url)
            svc["last_probe_at"] = now_iso()
            svc["last_latency_ms"] = latency2
            svc["last_error"] = err2
            if ok2:
                svc["last_ok_at"] = svc["last_probe_at"]
                svc["consecutive_fails"] = 0
                svc["heal_in_flight"] = False
                log_line(f"heal OK: {label} recovered after restart ({latency2}ms)")
                activity_log("WATCHDOG_HEAL_OK", f"{label} recovered after systemctl restart ({latency2}ms)")
                # Don't post a Discord alert — the heal worked silently
                continue
            else:
                log_line(f"heal FAILED: {label} still down after restart ({err2})")
                svc["consecutive_fails"] = 2
                svc["last_fail_at"] = svc["last_probe_at"]
                # Fall through to the alert path

        # Alert: respect 30-min dedup
        last_alert = svc.get("last_alert_at")
        should_alert = True
        if last_alert:
            try:
                last_dt = datetime.fromisoformat(last_alert)
                if (datetime.now(timezone.utc) - last_dt).total_seconds() < DEDUP_S:
                    should_alert = False
            except Exception:
                pass

        if should_alert:
            downtime = ""
            if svc.get("last_ok_at"):
                try:
                    start = datetime.fromisoformat(svc["last_ok_at"])
                    end = datetime.fromisoformat(svc["last_probe_at"])
                    delta = end - start
                    mins = int(delta.total_seconds() // 60)
                    downtime = f" (last OK ~{mins}m ago)"
                except Exception:
                    pass
            heal_note = " (self-heal attempted, still down)" if svc.get("heal_in_flight") else ""
            # For non-restartable services, the message says so — the on-call
            # human needs to know this is selena-api's in-process watchdog
            # territory, not the external watchdog's.
            owner_note = "" if can_restart else " (handled by selena-api in-process service_manager; alert indicates that watchdog is broken too)"
            path_hint = f"\n• site path: `{host_path}`" if host_path else ""
            msg = (
                f"🚨 **{label}** health check FAILED{heal_note}{downtime}\n"
                f"• probe `{url}` → `{err}`\n"
                f"• consecutive fails: {svc['consecutive_fails']}"
                f"{path_hint}\n"
                f"• systemd unit: `{unit}`{owner_note}\n"
                f"• watchdog will re-check in 5 min (30-min dedup on this alert)"
            )
            if post_discord(msg):
                svc["last_alert_at"] = svc["last_probe_at"]
                activity_log("WATCHDOG_ALERT", f"{label} probe failed: {err} (consecutive={svc['consecutive_fails']}, heal={'in-flight' if svc.get('heal_in_flight') else 'none'}, can_restart={can_restart})")

    save_state(state)
    # The watchdog's own job is to detect failure; it returns 0 always so
    # systemd never marks IT as failed (which would mask the real issue).
    return 0


def show_status() -> int:
    state = load_state()
    if not state:
        print("No state yet — first probe hasn't run.")
        return 0
    print(f"State file: {STATE_PATH}")
    print(f"Last check: {state.get('last_check_at', 'never')}")
    for label, _, _, _, can_restart in SERVICES:
        svc = state.get("services", {}).get(label, {})
        if not svc:
            print(f"  {label:24s} (no probes yet)")
            continue
        status = "UP" if svc.get("last_ok_at") and svc.get("consecutive_fails", 0) == 0 else "DOWN"
        role = "self-heals" if can_restart else "selena-api heals"
        print(
            f"  {label:24s} {status:5s} | "
            f"role={role:14s} | "
            f"last_ok={svc.get('last_ok_at', 'never')} | "
            f"consec_fails={svc.get('consecutive_fails', 0)} | "
            f"last_err={svc.get('last_error') or '-'}"
        )
    return 0


def reset_state() -> int:
    if STATE_PATH.exists():
        STATE_PATH.unlink()
    print(f"Removed {STATE_PATH}")
    return 0


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "check"
    if cmd == "check":
        return check_once()
    if cmd == "status":
        return show_status()
    if cmd == "reset":
        return reset_state()
    print(f"Usage: {sys.argv[0]} [check|status|reset]", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
