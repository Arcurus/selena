# Selena v2 - API Documentation

*Last Updated: 2026-06-04*

## Base URL
```
http://localhost:8765
```

## Authentication
All endpoints (except `/api/login`) require Bearer token authentication.
Include the token in the `Authorization` header:
```
Authorization: Bearer YOUR_TOKEN_HERE
```

---

## Authentication

### Login
**POST** `/api/login?password=PASSWORD`

Login to get an auth token.

> **Note:** Password is stored in `.env` file (WEB_PASSWORD variable).

**Response:**
```json
{
  "success": true,
  "token": "abc123..."
}
```

---

## LLM Call Tracking

### Get LLM Call Status
**GET** `/api/llm-calls`

Get current LLM call usage and limit. Legacy endpoint that reads
`data/llm_calls.json` directly — fast, but provider-agnostic. Prefer
`/api/llm-usage` for new code.

**Response:**
```json
{
  "used": 42,
  "limit": 4000,
  "remaining": 3958,
  "usage_percent": 1.1,
  "reset_info": "Token plan refreshes every 5 hours"
}
```

### Get Rich LLM Usage
**GET** `/api/llm-usage`

Multi-provider status: MiniMax token-plan quota per model, xAI / OpenRouter
local-only counters, per-provider 5h window, per-project allocation, polling
health, and any budget warnings. Backed by `code/llm_call_tracker.py`.

**Query parameters:**
- `sync=1` — force-refresh quotas from provider APIs before responding (slow,
  may rate-limit; default is to use the in-memory cache).

**Response (excerpt — see `llm_call_tracker.py:status()` for the full shape):**
```json
{
  "schema_version": 2,
  "window_hours": 5,
  "limits": { "hard_per_5h": 4500, "target_per_5h": 4000, "buffer_per_5h": 500 },
  "local": { "calls_5h": 0, "calls_1h": 0, "per_provider_5h": {}, "per_project_5h": {} },
  "providers": {
    "minimax": { "kind": "token_plan", "quota": { "ok": true, "models": { "general": { "window_5h": { "remaining_percent": 76 } }, "video": { "window_5h": { "remaining_percent": 100 } } } } },
    "xai":     { "kind": "oauth",      "quota": { "ok": false, "source": "local_only", "error": "no XAI_API_KEY in env" } }
  },
  "allocations": {
    "open-world-selena": { "allocated_5h": 900,  "used_5h": 0, "remaining_5h": 900 },
    "openlife":          { "allocated_5h": 2700, "used_5h": 0, "remaining_5h": 2700 },
    "selena":            { "allocated_5h": 450,  "used_5h": 0, "remaining_5h": 450 },
    "buffer":            { "allocated_5h": 450,  "used_5h": 0, "remaining_5h": 450 }
  },
  "warnings": []
}
```

### Force-Refresh Quotas
**GET** `/api/llm-usage/sync`

Always hits the provider APIs (MiniMax `/v1/token_plan/remains`, etc.) and
returns the refreshed view. Use sparingly.

### Get Time-Series Buckets
**GET** `/api/llm-usage/timeseries?hours=24`

Hourly buckets per provider plus a `total` line, for the web-UI chart.
`hours` is clamped to `[1, 168]` (1 hour to 1 week).

### Pre-Action Budget Check
**GET** `/api/llm-usage/budget?project=open-world-selena&additional=10`

Returns `ok: true|false` plus `remaining_5h_for_project`,
`global_remaining_5h`, and any active `warnings`. **Default project** is
`open-world-selena` (callers SHOULD pass `project=` explicitly).

### Pre-Action Gate
**GET** `/api/llm-usage/check?project=<p>&additional=<n>` (alias: `/api/llm-usage/gate`)

Per Arcurus 2026-06-03: "postpone autonomous / resource-intensive tasks until
the next refresh." Use this in any cron / sub-agent that burns > ~10 LLM
calls before doing the work. Returns `{ should_proceed, reason, retry_after_s, ... }`.

### Block Until Budget Allows
**GET** `/api/llm-usage/wait?project=<p>&additional=<n>&max-wait-s=1800`

Same as `check`, but blocks (polling, no LLM calls) until the budget allows
the action or `max-wait-s` elapses. Returns `waited_s`, `proceeded`, and the
underlying `check` payload.

### Budget Alert State
**GET** `/api/llm-usage/alert-state`

Read-only snapshot of the per-provider alert state (last fire time, current
level, etc.). For use in dashboards.

**GET** `/api/llm-usage/alert-test` — run the alert evaluator once and
return whether an alert fired.

**GET** `/api/llm-usage/alert-reset` — clear alert state for all providers.

### Record an LLM Call
**GET** `/api/llm-usage/record?provider=minimax&model=MiniMax-M3&project=selena-project&tokens_in=1200&tokens_out=400`

Used by the gateway / notifier to add a call to the local sliding-window
counter. `tokens_in` / `tokens_out` are optional.

### Pause / Resume a Provider
**GET** `/api/llm-usage/pause?provider=xai&seconds=1800&reason=quota_exhausted`

Stop polling a provider for `seconds` (max 86400). Useful when you know
credits are out and don't want the tracker to hammer the API.

**GET** `/api/llm-usage/resume?provider=xai` — clear the pause.

### Increment Legacy Counter
**GET** `/api/llm-calls/increment`

Bumps the `data/llm_calls.json` counter by 1. Backward-compat shim for
older callers; prefer `/api/llm-usage/record`.

---

## Todo / Loose Ends Management

### Get All Todos
**GET** `/api/todos?status=open&sort_by=priority`

Get all todos, optionally filtered by status and sorted.

**Parameters:**
- `status` (optional): Filter by status - `open`, `in_progress`, `done`
- `sort_by` (optional): Sort by - `priority` (default), `created`, `updated`

**Response:**
```json
{
  "todos": [
    {
      "id": "a5eda4d6",
      "short_desc": "Build todo web interface",
      "long_desc": "Display loose ends in web interface...",
      "priority": 9,
      "status": "open",
      "sensitive": false,
      "parent_id": null,
      "estimated_llm_calls": 10,
      "creator_id": "main",
      "conversation_id": "channel:1495170712397152367",
      "agent_id": "selena-v2",
      "block_reason": null,
      "waiting_for": null,
      "created_at": "2026-04-19T00:05:56.318686",
      "updated_at": "2026-04-19T00:05:56.318698"
    }
  ],
  "summary": {
    "total": 5,
    "open": 4,
    "in_progress": 0,
    "blocked": 0,
    "done": 1,
    "total_llm_calls": 50,
    "open_llm_calls": 30,
    "top_priority": [...]
  }
}
```

### Get Todo Summary
**GET** `/api/todos/summary?sensitive=true|false`

Get a quick summary of all todos.

**Parameters:**
- `sensitive` (optional): Filter by sensitive status - `true` or `false`

**Response:**
```json
{
  "total": 5,
  "open": 4,
  "in_progress": 0,
  "done": 1,
  "total_llm_calls": 50,
  "open_llm_calls": 30,
  "top_priority": [...]
}
```

### Add Todo
**POST** `/api/todos/add?short_desc=TITLE&long_desc=DESCRIPTION&priority=9&sensitive=false&parent_id=ID&estimated_llm_calls=10&creator_id=ID&conversation_id=ID&agent_id=ID`

Add a new todo.

**Parameters:**
- `short_desc` (required): Brief title (1-200 chars)
- `long_desc` (optional): Detailed description
- `priority` (optional): 1-10, 10 is highest (default: 5)
- `sensitive` (optional): If `true`, stored in `todos.env` NOT in git (default: false)
- `parent_id` (optional): Parent todo ID for hierarchical todos
- `estimated_llm_calls` (optional): Estimated LLM calls for this task
- `creator_id` (optional): ID of who created this todo
- `conversation_id` (optional): ID of the conversation this belongs to
- `agent_id` (optional): ID of the agent that owns this todo

**Response:**
```json
{
  "success": true,
  "todo": {
    "id": "abc123",
    "short_desc": "Build something",
    "long_desc": "Detailed description",
    "priority": 9,
    "status": "open",
    "sensitive": false,
    "parent_id": null,
    "estimated_llm_calls": 10,
    "creator_id": "main",
    "conversation_id": "channel:1495170712397152367",
    "agent_id": "selena-v2",
    "created_at": "2026-04-19T00:00:00.000000",
    "updated_at": "2026-04-19T00:00:00.000000"
  }
}
```

### Update Todo
**POST** `/api/todos/update?id=ID&short_desc=TITLE&long_desc=DESCRIPTION&priority=9&status=status&sensitive=true&parent_id=ID&estimated_llm_calls=20&creator_id=ID&conversation_id=ID&agent_id=ID&block_reason=REASON&waiting_for=TODO_ID`

Update an existing todo.

**Parameters:**
- `id` (required): Todo ID
- `short_desc` (optional): New title
- `long_desc` (optional): New description
- `priority` (optional): New priority (1-10)
- `status` (optional): New status (`open`, `in_progress`, `done`, `blocked`)
- `sensitive` (optional): If `true`, moves to `todos.env`
- `parent_id` (optional): New parent ID (use empty to remove hierarchy)
- `estimated_llm_calls` (optional): Updated estimate
- `creator_id` (optional): Updated creator ID
- `conversation_id` (optional): Updated conversation ID
- `agent_id` (optional): Updated agent ID
- `block_reason` (optional): Reason why blocked (only used when status is `blocked`)
- `waiting_for` (optional): ID of todo this is waiting for (only used when status is `blocked`)

**Response:**
```json
{
  "success": true,
  "todo": { ... updated todo ... }
}
```

### Get Children
**GET** `/api/todos/children?parent_id=ID`

Get all child todos of a parent todo.

**Parameters:**
- `parent_id` (required): Parent todo ID

**Response:**
```json
{
  "children": [
    {
      "id": "child123",
      "short_desc": "Subtask 1",
      "parent_id": "parent123",
      ...
    }
  ]
}
```

### Split Todo
**POST** `/api/todos/split?id=ID&subtasks=TASK1|||TASK2|||TASK3`

Split a big todo into smaller subtasks.

**Parameters:**
- `id` (required): Parent todo ID to split
- `subtasks` (required): Subtask titles separated by `|||` (or comma which auto-converts)

**Response:**
```json
{
  "success": true,
  "subtasks": [
    { "id": "sub1", "short_desc": "Subtask 1", "parent_id": "parent123", ... },
    { "id": "sub2", "short_desc": "Subtask 2", "parent_id": "parent123", ... }
  ]
}
```

### Mark Todo as Done
**POST** `/api/todos/mark-done?id=ID`

Mark a todo as done.

**Response:**
```json
{
  "success": true,
  "todo": { ... updated todo ... }
}
```

### Reload Todos from Disk
**GET** `/api/todos/reload` or **POST** `/api/todos/reload`

Force the in-memory todo list to re-read from `data/todos.json` and `data/todos.env`.
Useful after manual file edits (vim, scripts, backup restore) — the API server
caches its todos in memory at boot and won't see external file changes otherwise.

**Response:**
```json
{
  "success": true,
  "reloaded": { "regular": 621, "sensitive": 0, "stale": true },
  "was_stale": true
}
```

- `regular` / `sensitive` — number of todos loaded from each file
- `stale` — whether the on-disk file had changed since the last load
- `was_stale` — same as `reloaded.stale`, kept at the top level for convenience

_(Added 2026-06-05 per selena-project-worker to address loose-end todo `31e876a4`.)_

### Mark Todo as Blocked
**POST** `/api/todos/mark-blocked?id=ID&block_reason=REASON&waiting_for=TODO_ID`

Mark a todo as blocked with an optional reason and todo it's waiting for.

**Parameters:**
- `id` (required): Todo ID
- `block_reason` (optional): Reason why the todo is blocked
- `waiting_for` (optional): ID of the todo this is waiting for

**Response:**
```json
{
  "success": true,
  "todo": { ... updated todo ... }
}
```

### Unblock Todo
**POST** `/api/todos/unblock?id=ID`

Unblock a todo (sets status back to `open` and clears block_reason/waiting_for).

**Parameters:**
- `id` (required): Todo ID

**Response:**
```json
{
  "success": true,
  "todo": { ... updated todo ... }
}
```

### Delete Todo
**POST** `/api/todos/delete?id=ID`

Delete a todo (and optionally all its children).

**Response:**
```json
{
  "success": true
}
```

---

## Priority System

### Get Priority Tasks
**GET** `/api/priority/tasks`

Get all priority tasks.

### Get Top Priority
**GET** `/api/priority/top`

Get the top priority task.

### Add Priority Task
**POST** `/api/priority/add?name=TASK_NAME&description=DESCRIPTION&impact=9&urgency=7&effort=5&dependencies=3&learning=8&joy=7`

Add a new priority task with scores (1-10 each).

**Parameters:**
- `name` (required): Task name
- `description` (optional): Task description
- `impact` (optional): Impact score 1-10 (default: 5)
- `urgency` (optional): Urgency score 1-10 (default: 5)
- `effort` (optional): Effort score 1-10 (default: 5)
- `dependencies` (optional): Dependencies score 1-10 (default: 5)
- `learning` (optional): Learning score 1-10 (default: 5)
- `joy` (optional): Joy score 1-10 (default: 5)

### Clear All Tasks
**POST** `/api/priority/clear`

Clear all priority tasks.

---

## Knowledge Base

Knowledge base for lessons learned, skills, patterns, and references.

**Categories:** `lessons`, `skills`, `patterns`, `references`

### Get All Knowledge Entries
**GET** `/api/knowledge?category=lessons&search=error`

Get all knowledge entries, optionally filtered by category or search term.

**Parameters:**
- `category` (optional): Filter by category - `lessons`, `skills`, `patterns`, `references`
- `search` (optional): Search in title, content, and tags

**Response:**
```json
{
  "entries": [
    {
      "id": "abc123",
      "category": "lessons",
      "title": "Handle API errors gracefully",
      "content": "Always check return values and handle...",
      "tags": ["api", "error-handling"],
      "created_at": "2026-04-19T00:00:00",
      "updated_at": "2026-04-19T00:00:00"
    }
  ],
  "categories": {
    "lessons": 5,
    "skills": 3,
    "patterns": 2,
    "references": 1
  }
}
```

### Get Categories
**GET** `/api/knowledge/categories`

Get category counts.

**Response:**
```json
{
  "categories": {
    "lessons": 5,
    "skills": 3,
    "patterns": 2,
    "references": 1
  }
}
```

### Add Knowledge Entry
**POST** `/api/knowledge/add?category=lessons&title=TITLE&content=CONTENT&tags=tag1,tag2`

Add a new knowledge entry.

**Parameters:**
- `category` (required): Category - `lessons`, `skills`, `patterns`, `references`
- `title` (required): Entry title (1-200 chars)
- `content` (optional): Detailed content
- `tags` (optional): Comma-separated tags

**Response:**
```json
{
  "success": true,
  "entry": {
    "id": "abc123",
    "category": "lessons",
    "title": "Handle API errors gracefully",
    "content": "Always check return values...",
    "tags": ["api", "error-handling"],
    "created_at": "2026-04-19T00:00:00",
    "updated_at": "2026-04-19T00:00:00"
  }
}
```

### Update Knowledge Entry
**POST** `/api/knowledge/update?id=ID&title=NEW_TITLE&content=NEW_CONTENT`

Update an existing knowledge entry.

**Parameters:**
- `id` (required): Entry ID
- `title` (optional): New title
- `content` (optional): New content
- `category` (optional): New category
- `tags` (optional): Comma-separated new tags

**Response:**
```json
{
  "success": true,
  "entry": { ... updated entry ... }
}
```

### Delete Knowledge Entry
**POST** `/api/knowledge/delete?id=ID`

Delete a knowledge entry.

**Response:**
```json
{
  "success": true
}
```

---

## Self-Evolution Loop

### Get Evolution Status
**GET** `/api/evolution/status`

Get the status of the self-evolution loop.

**Response:**
```json
{
  "running": true,
  "interval_minutes": 10,
  "evolution_count": 5,
  "last_evolution": "Improved memory system",
  "system_health": {
    "api_server": true,
    "scheduler": true,
    "web_interface": true,
    "memory": true
  },
  "identified_improvements": [...],
  "recent_log": [...]
}
```

### Start Evolution Loop
**POST** `/api/evolution/start`

Start the self-evolution loop.

### Stop Evolution Loop
**POST** `/api/evolution/stop`

Stop the self-evolution loop.

### Trigger Evolution
**POST** `/api/evolution/trigger`

Trigger one evolution cycle immediately.

### Check System Health
**GET** `/api/evolution/health`

Check system health through the evolution loop.

---

## World Scheduler

### Get Scheduler Status
**GET** `/api/world/scheduler/status`

Get the status of the world scheduler.

**Response:**
```json
{
  "running": true,
  "interval_seconds": 30,
  "actions_per_cycle": 3,
  "last_action_time": "2026-04-19 00:00:00",
  "last_entity": "Shadow Crown",
  "last_outcome": "The Shadow Crown pulses with dark energy...",
  "action_count": 42,
  "error_count": 0,
  "world_name": "The realm of Shadows",
  "entity_count": 12,
  "world_action_count": 126,
  "open_world_url": "http://localhost:8080",
  "recent_log": [...]
}
```

### Start Scheduler
**POST** `/api/world/scheduler/start`

Start the world scheduler.

### Stop Scheduler
**POST** `/api/world/scheduler/stop`

Stop the world scheduler.

---

## Knowledge Base

The Knowledge Base stores lessons, skills, patterns, and reference information.

### Get Knowledge Entries
**GET** `/api/knowledge?category=lesson&search=keyword`

Get all knowledge entries, optionally filtered.

**Parameters:**
- `category` (optional): Filter by category - `lesson`, `skill`, `pattern`, `reference`
- `search` (optional): Search in title and content

**Response:**
```json
{
  "entries": [
    {
      "id": "abc123",
      "category": "lesson",
      "title": "Always check the API first",
      "content": "Before implementing, always check what the API expects...",
      "tags": ["api", "workflow"],
      "created_at": "2026-04-19T00:00:00.000000"
    }
  ],
  "categories": [
    {"name": "lesson", "count": 5},
    {"name": "skill", "count": 3},
    {"name": "pattern", "count": 2},
    {"name": "reference", "count": 1}
  ]
}
```

### Get Categories
**GET** `/api/knowledge/categories`

Get all knowledge categories with entry counts.

**Response:**
```json
{
  "categories": [
    {"name": "lesson", "count": 5},
    {"name": "skill", "count": 3},
    {"name": "pattern", "count": 2},
    {"name": "reference", "count": 1}
  ]
}
```

### Add Knowledge Entry
**POST** `/api/knowledge/add?category=lesson&title=TITLE&content=CONTENT&tags=tag1,tag2`

Add a new knowledge entry.

**Parameters:**
- `category` (required): Category - `lesson`, `skill`, `pattern`, `reference`
- `title` (required): Brief title
- `content` (required): The knowledge content
- `tags` (optional): Comma-separated tags

**Response:**
```json
{
  "success": true,
  "entry": {
    "id": "abc123",
    "category": "lesson",
    "title": "Test Lesson",
    "content": "This is a test lesson...",
    "tags": ["test"],
    "created_at": "2026-04-19T00:00:00.000000"
  }
}
```

### Update Knowledge Entry
**POST** `/api/knowledge/update?id=ID&title=TITLE&content=CONTENT&tags=tag1,tag2&category=lesson`

Update an existing knowledge entry.

**Parameters:**
- `id` (required): Entry ID
- `title` (optional): New title
- `content` (optional): New content
- `tags` (optional): New tags (comma-separated)
- `category` (optional): Move to different category

**Response:**
```json
{
  "success": true,
  "entry": { ... updated entry ... }
}
```

### Delete Knowledge Entry
**POST** `/api/knowledge/delete?id=ID`

Delete a knowledge entry.

**Parameters:**
- `id` (required): Entry ID

**Response:**
```json
{
  "success": true
}
```

---

## Cost Tracker

Daily / weekly LLM-cost reports. Backed by `code/cost_tracker.py`.

### Get Cost Report (JSON)
**GET** `/api/cost-tracker?weekly=0&date=2026-06-04`

Build a structured cost report for the given day (default = today) or, with
`weekly=1`, the current week. Returns `{ header, sections, data }`.

### Get Cost Report (Markdown)
**GET** `/api/cost-tracker/markdown?weekly=0&date=2026-06-04`

Same data, rendered as the Markdown that gets posted to `#cost-tracker`.

### Post Cost Report to Discord
**GET** `/api/cost-tracker/post?channel=<id>&date=2026-06-04&weekly=0`

Manually push the report to the given channel (default = the cost-tracker
channel from `~/.openclaw/openclaw.json`). Returns the rendered payload,
truncated to Discord's 2000-char limit.

---

## OpenClaw Usage Reconciler (cron-direct calls)

The cron `agentTurn` jobs (selena-project-worker, selena-open-world-worker,
selena-slow-heartbeat, etc.) run through the OpenClaw gateway and were
invisible to the cost tracker until 2026-06-05. The reconciler reads
`openclaw status --usage --json` and appends a synthesized event for every
session with non-null `totalTokens` that we haven't seen before, tagged
`project = "openclaw-direct"`. State is kept in
`data/reconcile_openclaw_state.json` (capped at 5,000 sessionIds).

CLI:  `python3 code/reconcile_openclaw_usage.py {poll,stats,reset-state,peek}`

Scheduled via systemd user timer (cheaper than an OpenClaw `agentTurn` cron
because no LLM call is involved):
```
~/.config/systemd/user/openclaw-usage-reconcile.service
~/.config/systemd/user/openclaw-usage-reconcile.timer   # OnUnitActiveSec=5min
ExecStart=~/openclaw/workspace/selena-project/scripts/reconcile_openclaw_usage.sh
```
Enable with: `systemctl --user enable --now openclaw-usage-reconcile.timer`.
Logs: `data/reconcile_openclaw.log` (last 200 lines, trimmed on each run).

The `openclaw-direct` allocation in `PROJECT_ALLOCATIONS`
(`code/llm_call_tracker.py`) is 0.05 (225 calls / 5h). Reduce other projects
if the cron calls exceed the cap.

---

## Discord Notifier

Per Arcurus 2026-06-03, every cron that posts to Discord SHOULD go through
these endpoints (or the `post-to-discord.sh` CLI) instead of OpenClaw's
delivery pipeline — the cron `announce` mode is broken with "Unsupported
channel" errors. All sends are logged to `data/discord_send_log.jsonl` with
`project` / `agent` / `task` tags for downstream stats.

### Notifier Status
**GET** `/api/discord/status`

Returns whether the notifier is enabled, the bot's username, the default
channel id, and a recent-send summary.

### Send a Message
**GET or POST** `/api/discord/send?channel=<id>&project=<p>&agent=<a>&task=<t>`

Send `text` to a channel. The text body can be passed as the `text=` query
parameter (GET) or as the raw request body (POST). `channel` is optional;
when omitted, falls back to the configured default. Returns
`{ success, channel_id, length, ... }`.

### Send Stats
**GET** `/api/discord/stats`

Aggregate counts from the send log: total sends, success rate, top
projects, top agents, last 24h / 7d / 30d.

### Recent Sends
**GET** `/api/discord/recent?limit=20`

Tail the send log. `limit` is clamped to `[1, 1000]`. Useful for
debugging "did the cron actually post?" questions.

---

## Cron Job Tracker

Mirror of the `code/cron_tracker.py` CLI. **All endpoints require auth.**
Mutating endpoints (`enable`, `disable`, `model`, `context`) call `log_api`
on every change.

### List Jobs
**GET** `/api/cron/list`

Returns every job from `~/.openclaw/cron/jobs.json` with its full metadata
(name, schedule, payload model, wake mode, delivery, last run, etc.).

### Job Status
**GET** `/api/cron/status?ref=<name|id>`

Summary for one job. `ref` can be the job's `id`, `name`, or any unique
substring. 404 if not uniquely matched.

### Enable / Disable
**GET** `/api/cron/enable?ref=<ref>`

**GET** `/api/cron/disable?ref=<ref>&reason=...`

Flip a job's `enabled` flag. Disabling also accepts a `reason` that gets
written to the job's metadata for later auditing.

### Set Model / Context
**GET** `/api/cron/model?ref=<ref>&model=minimax-portal/MiniMax-M3`

**GET** `/api/cron/context?ref=<ref>&max_tokens=8000`

Override the model or the maximum context length for a job. `max_tokens`
must be a positive integer.

### Read Instructions
**GET** `/api/cron/instructions?ref=<ref>`

Returns the `payload.message` (or `payload.text`) of a job — the system
prompt the agent sees when it wakes. Useful when you need to read the
current cron prompt without going through the gateway.

---

## World Backup

Daily snapshot of `open-world-selena/world_data/save.owbl` into
`selena-project/data/backups/save-daily-YYYYMMDD.owbl`, with 30-day rotation
and a Discord warning if the new backup suddenly loses > 50% of the previous
size (catches silent wipe / corruption). Backed by `code/world_backup.py`.

### Backup Status
**GET** `/api/world/backup/status`

Returns `count`, `newest`, `oldest`, `total_bytes`, `retention_days=30`,
`warn_ratio=0.5`.

### List Backups
**GET** `/api/world/backup/list`

Full list of backups with per-file `{ date_iso, path, size_bytes, age_days }`.

### Take a Backup Now
**GET** `/api/world/backup/run?channel=<id>&dry_run=0`

Run the daily backup immediately. `dry_run=1` returns what *would* happen
without touching the filesystem. `channel=<id>` overrides the warning
target.

---

## System Status

### Get System Status
**GET** `/api/status`

Get overall system status including all running services.

---

## Open World (Property Reference Docs)

### Get Property Docs (raw markdown)
**GET** `/api/openworld/property-docs`

Returns the Open World LLM property reference
(`open-world-selena/ai_templates/property_docs.md`)
as raw markdown — content-type `text/markdown; charset=utf-8`.

Served directly from the source file (single source of
truth — no copy in `selena-project/`), so future edits
to the file in the open-world-selena repo automatically
show up. No authentication required — public-readable
for browser convenience. This is the lowest-friction
path for "read the docs" in the browser.

Example: `http://selenaastra.com:8765/api/openworld/property-docs`

Response: the raw file content (4864 bytes as of 2026-06-08).

### Get Property Docs (JSON wrapper)
**GET** `/api/openworld/property-docs.json`

Same content as the raw endpoint, wrapped in a JSON
envelope with `path` / `last_updated` / `length` /
`content` fields. Auth required (Bearer token from
`/api/login`). For programmatic clients (the in-house
archive, a future "property-docs" tab in the web UI).

Response:
```json
{
  "path": "open-world-selena/ai_templates/property_docs.md",
  "last_updated": "2026-06-08T15:33:57+00:00",
  "length": 4864,
  "content": "# Entity Property Reference for the LLM\n..."
}
```

---

## Notes

- All timestamps are in ISO 8601 format
- All responses are JSON
- Error responses include an `error` field with error message
- The web interface is available at `http://localhost:8765/`
- Todos are persisted to `~/openclaw/workspace/selena/data/todos.json`
- LLM call tracking is stored in `~/openclaw/workspace/selena/data/llm_calls.json`
