# LEARNINGS.md

## 2026-06-03: Open World auto-save verification pattern

**What happened:** After deploying the auto-save fix (commit afddbfa, branch `fix/auto-save-on-process-action`), I needed to verify it was actually running in production — not just built and sitting on disk.

**What to do differently:**
- Always check `/proc/<pid>/exe` after a build+restart. If it shows `(deleted)`, the process is running the OLD binary that was overwritten on disk; `cargo build` alone does not reload it.
- Verify persistence by:
  1. `stat world_data/save.owbl` — check mtime is fresh (within last few minutes), not the build timestamp
  2. `xxd world_data/save.owbl | head` — confirm OWBL magic bytes (`0x4f 0x57 0x42 0x4c`) and world name in the header
  3. `ls -la world_data/` — confirm both `save.owbl` AND `save.owbl.bak` are present and being rotated
  4. Re-check mtime after a scheduler tick window to confirm growth or stable rotation
- systemd `Active: inactive (dead)` does not mean the process is dead — the binary can be running under a different parent (e.g., a stale-failed state from an old exit code). Verify with `ps -o pid,lstart,cmd -p <pid>`.
- `BinaryPersistence::save_world` was missing from `process_action_handler` in `open-world-selena/src/main.rs` — added at line 1622 in commit afddbfa. Logs the error but does not fail the API response (in-memory world stays authoritative for the running session).

**Verification cadence:** on every slow heartbeat for the first 24h after a persistence-related deploy, then weekly.

## 2026-06-03: Selena v2 API todo endpoint HTTP methods are non-REST

**What happened:** Tried to call `POST /api/todos/mark-done?id=7e108f3a` and `POST /api/todos/update?id=...&status=done` — both returned 404 "Not found" because the API server's `do_POST` only handles `/api/todos/add` (and reads JSON body, not query string). Wasted ~3 curl attempts before reading `code/api_server.py`.

**What to do differently:**
- `GET  /api/todos/mark-done?id=<id>` — mark done (despite the verb, this is a GET, not POST)
- `PUT  /api/todos/update?id=<id>&status=<status>` — update fields (PUT, not POST)
- `POST /api/todos/add` — JSON body required, NOT query string. Body: `{"short_desc": "...", "long_desc": "...", "priority": 5, "agent_id": "..."}`
- `do_GET` handles: `/api/todos`, `/api/todos/mark-done`, `/api/login`, `/api/llm-calls`, etc.
- `do_PUT` handles: `/api/todos/update` only
- `do_POST` handles: `/api/todos/add` only (JSON body)
- This is internally inconsistent (mark-done is a mutation via GET) but it IS the actual API surface — match it when scripting.

## 2026-06-03: API server in-memory state can desync from data/todos.json

**What happened:** Manually edited `data/todos.json` to mark a todo `done` — the API server's in-memory `self.todos` list still had the todo as `open` (it loaded at startup and only re-saves on API calls). A subsequent API save would clobber the manual edit. Re-synced by calling `GET /api/todos/mark-done?id=<id>` — the API loaded from disk at this point, then wrote the new state.

**What to do differently:**
- The API server (PID 11452) loads `data/todos.json` at startup and saves on every mutation. Manual file edits made while the API is running are NOT picked up.
- If you must edit `data/todos.json` directly (e.g., mass cleanup), either:
  1. Restart the API server after the edit (`systemctl --user restart selena-api.service` or kill+respawn)
  2. Make a follow-up API call (any mutation will trigger a `_save_todos()` which now includes the manual edit)
- The web interface also caches in-memory — same risk for the UI displaying stale state.
- This is a real "manual file edit clobbered by API" footgun, especially relevant for crons that may also be making API calls. Fix would be: have the API server reload from disk on a `?reload=1` query, or re-read on every read endpoint.

## 2026-06-03: Scheduler picks same entity when N=1

**What happened:** Open World has 1 entity (the world_clock). The selena-project scheduler at `code/scheduled_actions.py:188` does `entity = random.choice(entities)`. With N=1, every tick targets the same entity. The LLM is happy to invent "effects" for the system clock, including mutating protected int properties like `day`, `actions_today`, `has_history` to garbage values.

**What to do differently:**
- When the world has only system entities (world_clock), the scheduler should either:
  - Skip them entirely (`if entity_type == "world_clock": continue`)
  - Or apply only property writes that the LLM cannot break (read-only mode)
- The `process_action_handler` in `open-world-selena/src/main.rs` should validate effect targets: rejects writes to system entity types or to protected property keys.
- For now (P5, non-urgent), the auto-save fix correctly persists whatever the LLM writes — this is a feature-design question, not a runtime bug.

## 2026-04-20: mmx music generate vs mmx speech synthesize

**What happened:** I tried to create a "song" from a poem using `mmx speech synthesize`, but this creates spoken word audio (text-to-speech), not actual music.

**What to do differently:**
- `mmx speech synthesize` — Text-to-speech, converts text to spoken audio with a voice
- `mmx music generate` — Creates actual music with melody, harmony, and optionally lyrics

**How to create a song with mmx music generate:**
```bash
mmx music generate --prompt "description of music style and mood" --lyrics "the lyrics here" --out song.mp3
```
Or use `--instrumental` for music without lyrics.

**Example for a morning song:**
```bash
mmx music generate \
  --prompt "A gentle morning song, acoustic guitar and soft piano, ethereal vocals, dawn breaking over a quiet garden" \
  --lyrics "The dawn does not ask permission to arrive..." \
  --out morning_song.mp3
```


## 2026-06-03: /api/discord/send default channel is NOT #heartbeats

**What happened:** Sent the slow-heartbeat report via `GET /api/discord/send` without an explicit `channel=` param, expecting it to land in #heartbeats (1494781163498246144). It went to channel 1495170712397152367 instead — the default for any "selena-project"-named Discord channel resolved from `~/.openclaw/openclaw.json`. The previous run at 14:50 worked because the cron prompt's example explicitly included `channel=1494781163498246144`.

**Root cause:** `code/discord_client.py:default_channel_id` falls back to `channels.discord.guilds[].channels[]` where the channel name contains "selena-project" — that resolves to #selena-project (1495170712397152367), NOT #heartbeats.

**What to do differently:**
- **ALWAYS pass `channel=1494781163498246144` explicitly** when calling `/api/discord/send` from the slow-heartbeat (or any cron that targets #heartbeats).
- The default routing is fine for the fast heartbeat / selena-parent agents that actually want #selena-project or related channels.
- Cheaper than reading the notifier source on every run.

## 2026-06-08: project=selena-project filter currently returns 0 open todos

**What happened:** Ran the standard `GET /api/todos?status=open&project=selena-project&sort_by=priority` query from the selena-project-worker cron and got back an empty list (0 open + 0 in_progress). A manual scan of `data/todos.json` confirmed: 510 todos have `project=selena-project` but ALL are in completed/done/closed/blocked status. Meanwhile, 71 todos are open with `project=selena-project-2`, including 1 (`5b0825f1`, "Add Agents list to web UI") whose `agent_owner=selena-project-worker` — so the project field is wrong, not the agent_owner.

**Implication:** The trigger script's "N open todos" counter (which fires the worker) is including cross-project todos, so the selena-project-worker keeps getting triggered when there's actually no work in its strict project scope. The orchestrator has a known related issue tracked at todo `6931d51e` ("[Lunar] Orchestrator needs project-scoping + n…").

**What to do differently (in the selena-project-worker, until orchestrator is fixed):**
- After calling the `project=selena-project` API query, also do a direct `data/todos.json` scan for any `agent_owner=selena-project-worker` todos — those are "mine" even if the project tag is wrong. Decide case-by-case based on the long_desc whether the work is actually in selena-project scope (some are clearly lunar/coding-worker tasks that just got re-routed).
- If still 0 actionable todos: do small, safe selena-project housekeeping (worker_state hygiene, .learnings update, README review) and post a one-liner like "no open todos in my project scope; did <X> maintenance instead."
- Do NOT pick up `selena-project-2` todos just because the trigger fired — the cron explicitly says "Do NOT touch other projects (selena-project-2, openlife, open-world) unless explicitly told."
- The cached `todo.md` is stale (it showed 8 open, but only 7 blocked are real); trust the API / `data/todos.json`, not `todo.md`.

**Verification:** the trigger's `last_fire_reason` reads `"0 unprocessed sessions + 8 open todos"` for selena-project, but the cross-project-filtered query gives 0. This is the symptom.
