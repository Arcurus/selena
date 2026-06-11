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
2. **Self-heals on first failure**: calls `systemctl --user restart <unit>`, waits 6s, re-probes. If the restart fixed it, **no Discord alert** is posted (silent recovery).
3. **Alerts on persistent failure**: if the service is still down after the restart attempt, posts to **#selena-project-important** (channel `1495187458776891483`) with a 30-minute dedup window per service. Posts ONE alert, not spam.
4. **Recovery post**: when a previously-failed service comes back up, posts a `✅ ... is back UP` message (one-shot, no dedup loop).
5. **Always exits 0** — the watchdog's job is to detect failure, not to be one. A failed watchdog would mask the real issue.

## Files

| Path | Purpose |
|------|---------|
| `code/api_health_watchdog.py`         | The script. CLI: `check` (default), `status`, `reset`. |
| `~/.config/systemd/user/api-health-watchdog.service` | systemd oneshot, runs the script |
| `~/.config/systemd/user/api-health-watchdog.timer`  | fires every 5 min, persistent across reboots |
| `data/api_health_watchdog.json`       | state file: per-service last-OK, last-FAIL, last-alert, consecutive-fails, heal-in-flight |
| `data/api_health_watchdog.log`        | append-only probe + heal log |
| `data/activity_log`                   | receives `[WATCHDOG_HEAL_TRY]`, `[WATCHDOG_HEAL_OK]`, `[WATCHDOG_ALERT]`, `[WATCHDOG_RECOVERED]` one-liners |

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
| Single self-heal attempt on first fail | Avoids restart-loops. The 2nd consecutive fail is what triggers the alert. |
| 30-min alert dedup | If the API is down for 11h, we don't want 132 alerts in `#selena-project-important`. One alert per 30 min is enough. |
| 6s post-restart wait | Long enough for the systemd `RestartSec=5` to settle, short enough to not delay the timer. |
| Posts via `discord_client.post_to_channel` | Reuses the existing in-process Discord notifier (no new auth, no new bot). Already verified to handle 401s and rate limits gracefully. |
| Returns 0 on detected failure | The watchdog's own failure must not propagate to systemd (which would mask the real issue with "watchdog service failed"). |
| Uses `data/api_health_watchdog.json` not a global config | Keeps it self-contained inside selena-project, easy to find. |
| Covers 3 services, not just selena-api | open-world-selena is the biggest MiniMax consumer; orchestrator-status is the lunar workers' control plane. All three are critical. |

## What this watchdog does NOT do (deliberately)

- **Does not restart the gateway or Caddy** — those are infra-level, restarting them needs human judgment. The 3 services covered are in-VPS application services.
- **Does not post to #selena-project** — only #selena-project-important. The project channel is for work reports, not infra alerts.
- **Does not call the LLM** — adding an LLM call here would add 2-5s of latency + cost. Not worth it for a 200/health check.
- **Does not check the gateway (`openclaw-gateway`, port 18789)** — that's OpenClaw's concern, not selena-project's. If you want it covered, add it to `SERVICES` in the script.

## How to extend

Add a new service by appending to `SERVICES` in `code/api_health_watchdog.py`:

```python
SERVICES = [
    ("selena-api",          "selena-project.service",      "http://127.0.0.1:8765/api/health",     "/selena-astra/"),
    ("open-world-selena",   "open-world-selena.service",   "http://127.0.0.1:8081/api/world/stats", "/open-world/"),
    ("orchestrator-status", "orchestrator-status.service", "http://127.0.0.1:8766/health",         None),
    # Add more here: (label, systemd_unit, probe_url, host_path_or_None)
]
```

The `host_path` is only used in the alert message to help the on-call human navigate to the right URL. Set to `None` if the service has no public route.

## Related

- `scripts/smoke_test_selena_astra.sh` — one-shot HTTP smoke test (5 checks, no LLM, sub-second). Companion to the watchdog for "I just deployed, did I break it?" questions.
- `docs/selena-astra-page-regression-2026-06-11.md` — the doc that explains why relative paths matter under the `/selena-astra/` Caddy prefix, and the original incident report.
- `code/budget_gate_helper.py` — similar pattern (Python stdlib, state file, Discord dedup) but for the LLM cost gate, not the API health gate.
