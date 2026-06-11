# API Health Watchdog — `api-health-watchdog.py`

**Added 2026-06-11 per Arcurus #selena-project** ("yea add the watchdog script as you outlined. add it to the selena project").

## Why this exists

The `selena-project` API crashed on **2026-06-10 21:09:15 CEST** (systemd `status=1/FAILURE`, no useful traceback), went into `Start request repeated too quickly`, and stayed down for **~11 hours** until manually restarted at **2026-06-11 08:39:07 CEST** (PID 156380 at the time). Caddy returned 502 for every `/selena-astra/*` request during that window. Arcurus was hitting 502s from 21:09:33 onward.

The watchdog gap: `selena-fast-heartbeat` (the only cron that called `/api/health` every 5 min) had been **`enabled: false`** since 2026-05-31. The slow heartbeat is read-only and does not actively probe. So the 11h outage went unnoticed by Selena's own systems.

This watchdog closes that gap. It is **cheap** (no LLM, no OpenClaw agent session — just a Python script and a systemd timer), **fast** (probe takes <100ms per service), and **self-healing** (auto-restart on first failure before paging humans).

## What it does

Every 5 minutes (`OnUnitActiveSec=5min`), the watchdog:

1. **Probes three services in parallel-ish:**
   - `selena-api`         → `http://127.0.0.1:8765/api/health`
   - `open-world-selena`  → `http://127.0.0.1:8081/api/world/stats`
   - `orchestrator-status` → `http://127.0.0.1:8766/health`
2. **Self-heals on first failure — but only `selena-api`.** Calls `systemctl --user restart <unit>`, waits 6s, re-probes. If the restart fixed it, **no Discord alert** is posted (silent recovery).
3. **`open-world-selena` and `orchestrator-status` are NOT restarted by the external watchdog** — they're handled by the in-process `service_manager` running inside `selena-api` (see `code/service_manager.py`), which polls every 30s and restarts any service with `auto_start: true` in `docs/projects.md`. The external watchdog PROBES these services (so it can alert if selena-api's in-process watchdog is silently broken), but the restart is selena-api's job.
4. **Alerts on persistent failure**: if a service is still down after the restart attempt (selena-api) or after a full watchdog cycle (the other 2), posts to **#selena-project-important** (channel `1495187458776891483`) with a 30-minute dedup window per service. Posts ONE alert, not spam.
5. **Recovery post**: when a previously-failed service comes back up, posts a `✅ ... is back UP` message (one-shot, no dedup loop).
6. **Checks data freshness (added 2026-06-11 per Arcurus #cost-tracker, follow-up to the 22h silent-undercount incident):** `stat()`s the critical data files in `data/` and alerts (with the same 30-min dedup + recovery post pattern) if any of them go stale. This is the **second silent-failure layer** the HTTP probe can't catch: selena-api can be perfectly healthy while a producer cron (e.g. openclaw-usage-track) silently stops writing because someone pointed it at a dead shim. The 22h gap that motivated this extension would have been caught within 15 min if this check had existed.
7. **Always exits 0** — the watchdog's job is to detect failure, not to be one. A failed watchdog would mask the real issue.

## Files

| Path | Purpose |
|------|---------|
| `code/api_health_watchdog.py`         | The script. CLI: `check` (default), `status`, `reset`. |
| `~/.config/systemd/user/api-health-watchdog.service` | systemd oneshot, runs the script |
| `~/.config/systemd/user/api-health-watchdog.timer`  | fires every 5 min, persistent across reboots |
| `data/api_health_watchdog.json`       | state file: per-service and per-data-file last-OK, last-FAIL, last-alert, consecutive-fails, heal-in-flight |
| `data/api_health_watchdog.log`        | append-only probe + heal log |
| `data/activity_log`                   | receives `[WATCHDOG_HEAL_TRY]`, `[WATCHDOG_HEAL_OK]`, `[WATCHDOG_ALERT]`, `[WATCHDOG_RECOVERED]`, `[WATCHDOG_DATA_STALE]`, `[WATCHDOG_DATA_RECOVERED]` one-liners |
| `scripts/smoke_test_api_health_watchdog.py` | 7-test end-to-end smoke test for the data-freshness path (no real Discord posts, restores real mtimes) |
| `docs/projects.md`                    | **The source of truth for which services `selena-api`'s in-process watchdog auto-restarts.** Look here for the `auto_start: true` / `start_command` / `health_url` / `grace_period_seconds` / `max_restarts_per_hour` per service. Per Arcurus 2026-06-11 #selena-project: "all the other services can be tracked in project selena right? so a service can be registered there as auto start that we all added already". |

## Verified behavior (live test 2026-06-11 20:35 CEST)

1. Stopped `selena-project.service` via `systemctl --user stop`
2. Ran `python3 code/api_health_watchdog.py check`
3. Watchdog detected `URLError: [Errno 111] Connection refused` (`consecutive_fails=1`)
4. Auto-ran `systemctl --user restart selena-project.service` (rc=0)
5. Re-probed after 6s: `selena-api OK 2ms` → `consecutive_fails=0`, **no Discord alert** (silent self-heal)
6. Activity log shows full trace:
   ```
   [2026-06-11 18:35:17 UTC] [WATCHDOG_HEAL_TRY] attempting systemctl --user restart selena-project.service (consecutive_fails=1, err=URLError: [Errno 111] Connection refused)
   [2026-06-11 18:35:23 UTC] [WATCHDOG_HEAL_OK] selena-api recovered after systemctl restart (2ms)
   ```
7. **Total outage duration: 6 seconds** (vs. 11 hours for the 2026-06-10 incident)

The test of the **alert** path was also done with a fake unreachable service: it correctly posted a 268-char `🚨 fake-broken health check FAILED` message to channel 1495187458776891483 and recorded `[WATCHDOG_ALERT]` in the activity log.

## CLI

```bash
# Run one probe + heal cycle (what the timer fires)
python3 code/api_health_watchdog.py check

# Show last-known state for all 3 services
python3 code/api_health_watchdog.py status

# Clear state (force re-alert on next failure)
python3 code/api_health_watchdog.py reset
```

## systemd

```bash
# Verify timer is active
systemctl --user status api-health-watchdog.timer

# See next scheduled run
systemctl --user list-timers api-health-watchdog.timer

# Tail the journal (in addition to data/api_health_watchdog.log)
journalctl --user -u api-health-watchdog.service -n 20

# Manually trigger a check
systemctl --user start api-health-watchdog.service
```

## Design choices and trade-offs

| Choice | Rationale |
|--------|-----------|
| Pure Python (stdlib only, no `requests`) | Zero install footprint, runs anywhere Python 3.10+ is present. |
| `urllib.request` instead of `curl` | No subprocess, no shell-quoting, easier to reason about timeouts. |
| 5-min cadence (matches old fast-heartbeat) | Frequent enough to catch outages before humans notice, sparse enough to not generate noise. |
| **External watchdog restarts ONLY `selena-api`** (per Arcurus 2026-06-11 #selena-project) | The other 2 services (`open-world-selena`, `orchestrator-status`) are registered with `auto_start: true` in `docs/projects.md`, so `selena-api`'s in-process `service_manager` heals them. The external watchdog is a safety net for the orchestrator itself. |
| Probes all 3 services (not just selena-api) | Even though it only restarts 1, it PROBES all 3 so it can alert if the in-process watchdog is silently broken. |
| Single self-heal attempt on first fail | Avoids restart-loops. The 2nd consecutive fail is what triggers the alert. |
| 30-min alert dedup | If the API is down for 11h, we don't want 132 alerts in `#selena-project-important`. One alert per 30 min is enough. |
| 6s post-restart wait | Long enough for the systemd `RestartSec=5` to settle, short enough to not delay the timer. |
| Posts via `discord_client.post_to_channel` | Reuses the existing in-process Discord notifier (no new auth, no new bot). Already verified to handle 401s and rate limits gracefully. |
| Returns 0 on detected failure | The watchdog's own failure must not propagate to systemd (which would mask the real issue with "watchdog service failed"). |
| Uses `data/api_health_watchdog.json` not a global config | Keeps it self-contained inside selena-project, easy to find. |

## What this watchdog does NOT do (deliberately)

- **Does not restart the satellite services** (`open-world-selena`, `orchestrator-status`). Those are `selena-api`'s responsibility, via its in-process `service_manager`. The external watchdog only restarts `selena-api` itself.
- **Does not auto-restart producer crons on data-staleness** — the producer crons (openclaw-usage-track, llm-usage-sync) have their own systemd timers. The watchdog's job is to detect that a producer is broken; the human decides what to do (re-point at the right script, restart the unit, etc.). Auto-restarting a misconfigured cron just makes it fail in a loop.
- **Does not restart the gateway or Caddy** — those are infra-level, restarting them needs human judgment. The 3 services covered are in-VPS application services.
- **Does not post to #selena-project** — only #selena-project-important. The project channel is for work reports, not infra alerts.
- **Does not call the LLM** — adding an LLM call here would add 2-5s of latency + cost. Not worth it for a 200/health check.
- **Does not check the gateway (`openclaw-gateway`, port 18789)** — that's OpenClaw's concern, not selena-project's. If you want it covered, add it to `SERVICES` in the script.

## Data freshness: the second silent-failure layer (added 2026-06-11)

**The problem this solves:** on 2026-06-10, `code/openclaw_usage.py` was converted to a re-export shim but the systemd timer kept calling it as a script. The shim exited 0 with no log, no work. The HTTP probe said selena-api was up (it was). The `data/openclaw_usage.jsonl` silently stopped writing for **22 hours** until Arcurus noticed by hand. An HTTP probe alone is necessary but not sufficient: a producer cron can silently die while the API stays up.

**How it works:** the watchdog `stat()`s the files in `DATA_FILES` and applies the same alert pattern as for services (30-min dedup, recovery post, no auto-heal):

| File | Threshold | Producer | Notes |
|------|-----------|----------|-------|
| `data/llm_usage_events.jsonl` | 30 min | in-process `/api/llm-usage/record` + openclaw-usage-reconciler.timer (5-min cadence) | Threshold is wider than the producer cadence because the reconciler only writes for **new sessions** (deduped by sessionId). 30 min allows for legitimate low-traffic hours. |
| `data/openclaw_usage.jsonl` | 15 min | openclaw-usage-track.service (5-min cadence) | This is the file that went silent for 22h. Every cron run writes, so 15 min = 3 missed cycles = alert. |
| `data/llm_usage_snapshot.json` | 20 min | llm-usage-sync.timer (scripts/sync_llm_usage.sh, 10-min cadence) | Wider than the cadence to allow for the script's multi-step refresh (minimax + openai + status). |

**Smart context for the events file:** when `llm_usage_events.jsonl` is stale, the alert message also checks `openclaw_usage.jsonl`:
- If openclaw-usage is **also** stale → "system appears genuinely quiet" hint
- If openclaw-usage is **fresh** → "openclaw_usage.jsonl is fresh — sessions ARE happening, the reconciler may just not have seen new ones yet (low-traffic hour?)" hint

This saves the human from waking up at 3am to investigate a "stale data" alert that's actually just a quiet hour.

**Alert message format:**

```
🚨 openclaw_usage.jsonl data is STALE (last fresh ~22h ago)
• current mtime age: 79200s (threshold: 900s)
• path: /home/openclaw/.../data/openclaw_usage.jsonl
• producer: openclaw-usage-track.service (5-min timer; cmd=openclaw_cost_tracker.py sync)
• consecutive stale checks: 3
• watchdog will re-check in 5 min (30-min dedup on this alert)
```

**Recovery message:**

```
✅ openclaw_usage.jsonl is fresh again (stale ~22h)
• path: /home/openclaw/.../data/openclaw_usage.jsonl
• current age: 12s (threshold: 900s)
```

## How to extend

Add a new service by appending to `SERVICES` in `code/api_health_watchdog.py`:

```python
SERVICES = [
    ("selena-api",          "selena-project.service",      "http://127.0.0.1:8765/api/health",     "/selena-astra/"),
    ("open-world-selena",   "open-world-selena.service",   "http://127.0.0.1:8081/api/world/stats", "/open-world/"),
    ("orchestrator-status", "orchestrator-status.service", "http://127.0.0.1:8766/health",         None),
    # Add more here: (label, systemd_unit, probe_url, host_path_or_None, can_restart)
]
```

The `host_path` is only used in the alert message to help the on-call human navigate to the right URL. Set to `None` if the service has no public route.

Add a new data file by appending to `DATA_FILES`:

```python
DATA_FILES = [
    # (label, relpath, max_age_seconds, producer_hint)
    ("my_data.jsonl", "data/my_data.jsonl", 600, "my-producer.service (5-min timer)"),
    # ...
]
```

The `max_age_seconds` should be ~2x the producer's expected cadence + 5 min grace. The `producer_hint` shows up in the alert message so the on-call human knows what to investigate.

## Related

- `scripts/smoke_test_selena_astra.sh` — one-shot HTTP smoke test (5 checks, no LLM, sub-second). Companion to the watchdog for "I just deployed, did I break it?" questions.
- `docs/selena-astra-page-regression-2026-06-11.md` — the doc that explains why relative paths matter under the `/selena-astra/` Caddy prefix, and the original incident report.
- `code/budget_gate_helper.py` — similar pattern (Python stdlib, state file, Discord dedup) but for the LLM cost gate, not the API health gate.
