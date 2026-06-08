# Worker & Trigger Architecture

> **Per Arcurus 2026-06-07 #cost-tracker** — overview of how the
> selena-project worker / trigger / budget-gate system fits together.
> This is the authoritative reference; the README points here.

---

## TL;DR

Every 5 minutes, a small `worker-trigger` script decides whether to fire
a project worker. It looks at four things:

1. **Budget gate** — does MiniMax have headroom right now?
2. **Worker already running?** — is the same worker cron live in the gateway?
3. **Unprocessed messages or open todos?** — is there actually work to do?
4. **30-min debounce** — did any Selena post happen in the working channel
   in the last 30 minutes?

If all four say "yes / green", the script fires the worker via
`openclaw cron run <id>` (the same CLI the moderation cron trigger uses).
The worker itself then posts to its working channel and updates
`data/worker_state.json[channel_id].last_worker_message_at` so the
30-min debounce works going forward.

The whole thing is **autonomous** — no human in the loop — but every
piece of state is observable in the web UI (http://selenaastra.com:8765/)
and every trigger is logged.

---

## Components

### 1. Worker crons (in `~/.openclaw/cron/jobs.json`)

Worker crons are **scheduled to fire only once per day** in the early
morning (4, 5, 6 AM CET) on their own schedule. They are NOT the
primary fire mechanism — the trigger script is. The cron schedule
exists as a safety net for when the trigger script is down or the
budget is closed-95 for >24h.

There are two flavors:

| Flavor | Schedule (per-worker) | What runs it | Example |
|--------|----------|--------------|---------|
| `triggerable` | **4 AM CET** (selena-project-worker), **5 AM CET** (selena-project-lunar-worker), **6 AM CET** (selena-open-world-worker) | The trigger script (`worker-trigger.timer`) decides when to fire. | selena-project-worker, selena-project-lunar-worker, selena-open-world-worker |
| `ALWAYS-SCHEDULED` | varies (e.g. `0 4 * * *`) | The cron's own systemd timer fires it on schedule. The trigger script **does not fire** these — they have `worker_cron_schedule_note` starting with `ALWAYS-SCHEDULED`. | openlife-moderation-check |

**Why 4/5/6 AM:** the three triggerable workers fire on a one-hour
stagger in the early morning (per Arcurus 2026-06-08 #openworld),
spread out so the LLM budget (5h reset window) is shared, not
bunched. The order is: meta/coordinator first (selena-project at
4 AM), then the lunar subproject (5 AM), then the heaviest
(open-world-selena at 6 AM). The 60s stagger between them is
preserved via `staggerMs: 60000` in the cron job config.

`ALWAYS-SCHEDULED` is a tag in `data/project_mapping.json[*].worker_cron_schedule_note`.
The trigger script reads it and skips those projects entirely. They run
on their own cadence and only check the 95% gate (won't actually do
work when the gate is closed-95 — but they still log their intent so
we know they tried).

**Why this split exists:** moderation checks (e.g. openlife's) are
critical — they protect the Discord community. They MUST run on a
fixed schedule even if the trigger script is broken or the budget is
closed. Triggerable workers (selena-project, open-world-selena) are
non-critical productivity work; they can wait until conditions are
right.

### 2. `worker-trigger.timer` → `worker-trigger.service`

systemd timer that fires every 5 minutes. Each run:

1. Reads the budget gate from `data/budget_gate.json`
2. Walks all projects in `data/project_mapping.json`
3. For each triggerable project, runs the 4-step check (see TL;DR)
4. Fires the worker if conditions are met (via `openclaw cron run <id>`)
5. Updates `data/worker_state.json` with last_triggered_at, trigger_count, last_fire_reason

**Files:**
- Source: `code/worker_trigger.py` (~1000 lines)
- Timer unit: `~/.config/systemd/user/worker-trigger.timer`
- Service unit: `~/.config/systemd/user/worker-trigger.service`
- Log: `data/worker_trigger.log` (append-only)
- State: `data/worker_state.json` (per-channel + per-project rollup)

**CLI subcommands:**
```bash
python3 code/worker_trigger.py status             # current state of all projects
python3 code/worker_trigger.py check              # dry-run: what WOULD fire
python3 code/worker_trigger.py trigger --project X --force  # force fire (bypasses checks)
python3 code/worker_trigger.py trigger-all       # 5-min cycle
python3 code/worker_trigger.py sync-todo-md      # sync <project>/todo.md from data/todos.json
python3 code/worker_trigger.py mark-processed    # manually set last_processed_at = now
python3 code/worker_trigger.py context           # show the context that would be passed
python3 code/worker_trigger.py list-jobs         # list triggerable crons
```

### 3. `budget-gate.timer` → `budget-gate.service`

systemd timer that fires every 5 minutes. Each run:

1. Calls `mmx quota` (MiniMax Token Plan quota API)
2. Parses the response into a 5h-window state machine:
   - `open` (used < 80% of 5h quota)
   - `closed-80` (used >= 80% — warning)
   - `closed-95` (used >= 95% — trigger script blocks, only ALWAYS-SCHEDULED runs)
3. Writes `data/budget_gate.json` with the current state

**Files:**
- Source: `code/budget_gate.py` (~130 lines)
- State: `data/budget_gate.json` (schema_version, state, used_pct, api_window, last_open_at, last_closed_at)

**Why 80% and 95%:**
- 80% — soft warning, "be careful, budget tight"
- 95% — hard block, "stop everything, only critical things run"
- The 5h window matches MiniMax's quota interval exactly (so we're
  counting the same calls MiniMax is counting)

### 4. The trigger checks (evaluated in order; first to fail wins)

The trigger is a sequence of **yes/no checks**, each of which can
return a `skipped` reason if it fails. The first one to fail stops
the trigger; the order below is the order they're evaluated in
`code/worker_trigger.py:_evaluate_project_trigger()`. If all say
green → fire the worker via `openclaw cron run <id>`.

#### 0. ALWAYS-SCHEDULED short-circuit (per-project)

```
If project_mapping.json[*].worker_cron_schedule_note starts with
"ALWAYS-SCHEDULED", skip with reason
  "ALWAYS-SCHEDULED: trigger script leaves it to the timer's own schedule"
```

The trigger script does NOT fire these. They run on their own
systemd timer. See section 1 for the schedule split.

#### 1. Budget gate OK?

```
state == "open" (i.e. used < 80% of 5h quota)
Note: ALWAYS-SCHEDULED crons check 95% gate, not 80%.
Source: data/budget_gate.json (refreshed by budget-gate.timer every
5 min via the MiniMax Token Plan API).
```

If `gate.state != "open"`, skip with reason
`gate-closed (used X%)`.

#### 2. Worker already running?

```
Look at openclaw_usage.jsonl for a session with this cronJobId
and updatedAt within the last RUNNING_TIMEOUT_S (1h).
Also: the gateway's own "already-running" check is the
authoritative guard; if the gateway returns
{"ran": false, "reason": "already-running"} we treat it as
success (the worker IS in flight, our trigger was effective).
```

If a session updated in the last 1h, skip with reason
`worker-running (cron X has a session updated in the last 3600s)`.

#### 3. Unprocessed messages OR open todos?

```
Unprocessed = sessions in data/openclaw_usage.jsonl that targeted
this worker's channel AFTER the last_processed_at timestamp for
this channel.
Open todos = todos in data/todos.json with project=<this> and
status NOT in (done, completed, closed, cancelled).
If BOTH are zero, skip (no work to do).
```

If nothing to do, skip with reason
`no-work (no unprocessed messages and no open todos for this project)`.

#### 4a. Channel active — session event? (openclaw_usage.jsonl, 30 min)

```
Look at the most recent session START/UPDATE/END event in
data/openclaw_usage.jsonl for this worker's channel. If it was
within the last 30 min, skip — the channel is "active" because
a Selena session is currently in flight (or just finished).

This is the OLDER check (added 2026-06-08, todo 434e6755). It
catches "Selena is currently working" — a worker session that
just started but hasn't posted yet would not be caught by the
debounce (4c below), so we keep this layer.
```

If a session event in the last 30 min, skip with reason
`channel-recently-active (any session event in this channel
less than 30 min ago, last at <timestamp>)`.

#### 4b. Channel active — any message? (Discord REST API, 30 min)

```
Hit Discord's REST API:
  GET /api/v10/channels/{channel_id}/messages?limit=1
If the response's first message (most recent) has a timestamp
within the last 30 min, skip — the channel is "active" because
someone posted.

This is the NEWER check (added 2026-06-08, todo 434e6755 update
per Arcurus: 'yes ship it. it should not fire if it saw a message
in its working channel in the last 30 mins'). It catches raw
Arcurus messages that don't trigger a Selena session — which
4a misses (4a only sees session events, not raw Discord posts).
The Discord token is resolved from $DISCORD_BOT_TOKEN (preferred)
or ~/.openclaw/openclaw.json (fallback). The result is cached
per-channel for 60s.

Graceful degradation: if the token can't be read, the API call
fails, or the response is malformed, the function returns None
and this check is a no-op. Better to occasionally fire when
the channel is active than to silently stop firing.

Why we keep BOTH 4a and 4b: they cover different signals.
  4a: "a Selena session is in flight" (worker mid-run,
      main-Selena LLM turn in progress)
  4b: "any message in the channel" (Arcurus typing, the
      worker's own posts, messages from other bots, etc.)
4a alone misses the "Arcurus messaged and the worker has
nothing to do" case (4b). 4b alone misses the "Selena
session is in progress but hasn't posted yet" case (4a).
Keeping both gives full coverage.
```

If a message in the last 30 min, skip with reason
`channel-last-message-recent (any message in this channel
less than 30 min ago, last at <timestamp>)`.

#### 5. 30-min debounce?

```
Look at the most recent Selena post in this channel (any agent,
any kind). If it was <30 min ago, skip — the channel is "active"
and we don't want to spam (pile up after the worker has already
posted).

The worker itself updates last_worker_message_at after posting
so the debounce works on the very next trigger cycle.
```

If a worker post in the last 30 min, skip with reason
`debounce (worker posted in this channel less than 30 min ago,
last at <timestamp>)`.

#### Summary table

| # | Check | Skip reason | Data source | Why we have it |
|---|---|---|---|---|
| 0 | ALWAYS-SCHEDULED? | `always-scheduled` | `project_mapping.json` | Moderation-style crons are on their own schedule |
| 1 | Budget gate open? | `gate-closed` | `data/budget_gate.json` | Don't burn tokens on a closed budget |
| 2 | Worker running? | `worker-running` | `openclaw_usage.jsonl` (session event in last 1h) | Don't fire a duplicate worker |
| 3 | Any work? | `no-work` | `openclaw_usage.jsonl` + `data/todos.json` | Don't waste an LLM call when there's nothing to do |
| 4a | Channel active (session)? | `channel-recently-active` | `openclaw_usage.jsonl` (last 30 min) | Don't wake a worker that's already mid-session |
| 4b | Channel active (any message)? | `channel-last-message-recent` | Discord REST API (`/channels/{id}/messages?limit=1`, last 30 min) | Don't fire when Arcurus is actively chatting |
| 5 | Worker just posted? | `debounce` | `data/worker_state.json` (last_worker_message_at) | Don't pile up after posting |

If all 7 say green → fire the worker via `openclaw cron run <id>`.

### 5. `data/worker_state.json` (single source of truth)

Two-level structure: per-channel + per-project rollup.

```json
{
  "schema_version": 1,
  "updated_at": "2026-06-07T22:30:00+00:00",
  "channels": {
    "<channel_id>": {
      "project": "selena-project",
      "worker_cron_id": "ea0aa9e8-…",
      "last_processed_at": "2026-06-07T22:25:00+00:00",
      "last_worker_message_at": "2026-06-07T22:25:00+00:00",
      "last_triggered_at": "2026-06-07T22:25:00+00:00",
      "trigger_count": 5,
      "last_skip_reason": "debounce: any Selena post in this channel less than 30 min ago"
    }
  },
  "projects": {
    "selena-project": {
      "primary_channel_id": "1495170712397152367",
      "worker_cron_id": "ea0aa9e8-…",
      "last_triggered_at": "2026-06-07T22:25:00+00:00",
      "trigger_count": 5,
      "last_fire_reason": "1 unprocessed sessions + 35 open todos"
    }
  }
}
```

**Who writes to this file:**
- `worker_trigger.py` writes `last_processed_at`, `last_triggered_at`, `trigger_count`, `last_fire_reason`, `last_skip_reason`, the project rollup
- The **worker itself** writes `last_worker_message_at` (after posting in step 6b of its prompt). The trigger script does NOT update this — only the worker does, because the worker is the one that knows if its post actually went out and any loose ends were handled.

**Why split the writes:**
If the trigger script updated `last_worker_message_at` based on "I
fired the worker" alone, we'd accidentally debounce on a fire that
crashed before posting. By having the worker update it AFTER it posts
and finishes step 6b (loose ends + todo update), we know the post
landed AND the worker cleaned up before we consider the channel "active
again".

### 6. `data/worker_context/<cron_id>.md`

A markdown file the trigger script writes for each fire. Contains:
- The project name and working channel id
- The source-of-truth todo file (`data/todos.json`) and per-project
  scratchpad (`<project>/todo.md`) paths
- Instructions for the worker to update `data/worker_state.json` after posting
- The list of unprocessed messages (session id + timestamp)
- The list of open todos for this project

The worker's prompt prepends this context, so the worker always knows
which session to look at, which todos to pick from, and which state
file to update.

### 7. `<project>/todo.md` (per-project scratchpad)

Synced from `data/todos.json` (the central source of truth) every time
the trigger fires AND on a separate `sync-todo-md` subcommand. The
worker can read EITHER file, but `data/todos.json` is authoritative —
the worker should write todos back to the central file via the API:

```bash
# Mark a todo completed:
curl -X POST "http://127.0.0.1:8765/api/todos/update?id=<id>&status=completed"

# Mark a todo blocked:
curl -X POST "http://127.0.0.1:8765/api/todos/update?id=<id>&status=blocked&block_reason=<why>"

# Add a new todo:
curl -X POST -H "Content-Type: application/json" \
  -d '{"short_desc":"...", "long_desc":"...", "priority":5, "project":"<this>", "agent_owner":"<your-name>"}' \
  "http://127.0.0.1:8765/api/todos/add"
```

---

## Data flow

```
+--------------------+        +-----------------------+
|  Apps / Cron jobs  |        |  OpenClaw session     |
|  (scheduled_       |        |  transcripts          |
|  actions.py,       |        |  (data/openclaw_      |
|  open-world Rust)  |        |  usage.jsonl)         |
+----------+---------+        +----------+------------+
           |                             |
           | /api/llm-usage/record       | parsed by
           | (direct API call)           | openclaw_usage.py
           v                             v
   +-----------------+         +---------------------+
   | llm_call_       |         | code/openclaw_      |
   | tracker.py      |         | usage.py            |
   | (OLD tracker)   |         | (NEW tracker)       |
   +--------+--------+         +----------+----------+
            |                             |
            | writes to:                 | writes to:
            v                             v
    +-------------------------------------------+
    |     data/llm_usage_events.jsonl           |  <-- single source of truth
    |     (per-call log: one row per LLM call)  |
    +-------------------+-----------------------+
                        |
            +-----------+------------+
            |                        |
            v                        v
    +----------------+      +-------------------+
    | cost_tracker.py|      | /api/openclaw-    |
    | (daily report) |      | usage/per-call-   |
    +----------------+      | stats             |
                            +---------+---------+
                                      |
                                      v
                            +-------------------+
                            | web/index.html    |
                            | (LLM usage card)  |
                            +-------------------+
```

### The 7-step worker prompt (what every triggerable worker does)

The 25h cron schedule at `0 5 * * *` (or similar) is a SAFETY NET. The
trigger script is the primary fire mechanism. When a worker fires, it
runs this exact 7-step prompt (in the cron job's `payload.message`):

1. **Budget gate** — `python3 code/cost_tracker.py status`. If used >= 80%, post a one-liner to #selena-project and exit.
2. **Channel-active check** — if the last message in the working channel is < 20 min old, exit silently.
3. **Sync loose ends from channel** — read recent messages, add Arcurus's loose ends to `data/todos.json` (or mark completed ones).
4. **Pick up a high-prio todo** — top of the open queue. Do one piece of work, mark completed or blocked.
5. **Pick up a low-prio todo** — same as step 4 but P1-P4.
6. **Post to working channel** — `selena-project-worker @ HH:MM CET: <summary>`.
7. **(Step 6b — added 2026-06-07)** **Update `data/worker_state.json[channel_id].last_worker_message_at = now()`.** This is what makes the 30-min debounce work going forward.

### How the trigger fires a worker

```python
# Trigger script
subprocess.run(["openclaw", "cron", "run", "<cron_id>"], timeout=30)
# Returns: {"ok": true, "enqueued": true, "runId": "manual:..."}
# Or:     {"ok": true, "ran": false, "reason": "already-running"}
#   (which we treat as success — the gateway is already running it)
```

The moderation cron uses the same pattern in
`/api/discord-lookup/trigger-cron` → `openclaw cron run <id>`. The
gateway then enqueues the worker's session and starts it asynchronously
in its own process space. The trigger script doesn't wait — it returns
in <3s.

---

## Web UI: 🚀 Worker Triggers card

Located in the OpenClaw Usage section of http://selenaastra.com:8765/.

**Meta line:** budget gate state, % used, time to reset, debounce
minutes, running-timeout seconds, **"last updated Xs ago"** (added
2026-06-07; ticks up every 30s while the tab is open).

**View toggle:** Projects / Crons / Channels — three ways to slice the
same data.

**Per-project row:**
- Project emoji + name
- Worker cron name (or `always-scheduled` tag for ALWAYS-SCHEDULED)
- Unprocessed messages count
- Open todos count
- Last triggered (Xm ago)
- Last worker message (Xm ago) — set by the worker itself in step 6b
- Last processed (Xm ago)
- Trigger count
- 🚀 Trigger button (or "always-scheduled" if ALWAYS-SCHEDULED)
- Last skip reason (if any)

**Force worker buttons** (replaced the old "open / close-95" gate
override on 2026-06-07 per Arcurus): per-project buttons that bypass
the gate, debounce, and "worker running" checks. Useful when Arcurus
wants to force a fire even if the budget is closed-95.

---

## Failure modes and how to recover

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| Worker not firing | Budget gate is closed-95 | `python3 code/budget_gate.py pull` to refresh; check `mmx quota` directly |
| Worker fires but doesn't post | Worker session crashed | `journalctl --user -u openclaw-gateway` for the run output |
| Worker posts but trigger fires again 5 min later | Worker didn't update `last_worker_message_at` in step 6b | Check the worker's stderr for the python3 -c ... command; fix the cron prompt if the instruction is missing |
| Channel shows >100 unprocessed messages | `last_processed_at` is stale | `python3 code/worker_trigger.py mark-processed --channel <id>` to reset |
| Trigger script segfaults | Bug; check `data/worker_trigger.log` | `systemctl --user restart worker-trigger.timer` |
| `already-running` returns repeatedly | Worker is taking >1h | Check what the worker is doing; consider killing the run via `openclaw cron` (no built-in cancel, but `kill <pid>` on the gateway process) |

---

## Related docs

- [selena-v2-architecture.md](./selena-v2-architecture.md) — high-level
  overview of the selena-project API server + crons + web UI
- [API.md](./API.md) — all HTTP endpoints, including
  `/api/openclaw-usage/workers/*`
- [visualization.md](./visualization.md) — what each card on the web
  UI shows and the data path
- [moderation.md](./moderation.md) — the moderation cron, which is
  the example for the `openclaw cron run` CLI trigger pattern
- [services.md](./services.md) — all systemd services, including
  `worker-trigger.timer` and `budget-gate.timer`
- [MEMORY.md](../../MEMORY.md) — long-term memory with operational
  patterns and the budget gate / 5h window context
