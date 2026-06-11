# Selena v2 — Internal Architecture

> **Audience:** operator reference. The **internals** of Selena v2
> — the memory hierarchy, the context loop, the data structures.
> **NOT** loaded in the LLM prompt — for the operator-facing
> identity (tracking + statistics + transparency + web UI),
> see [`docs/transparency-and-stats.md`](transparency-and-stats.md).
>
> The two docs are complementary: this one is the *internals*,
> the other is the *exposure*.

## Identity (per Arcurus 2026-06-08)

**Selena v2 is for now mainly focused on adding tracking
and statistics, and providing transparency inside and
outside in the form of a web interface.**

That's the one-line identity. The 4 main subsystems
(tracking, statistics, transparency, web UI) and the
surface they expose are documented in
[`transparency-and-stats.md`](transparency-and-stats.md).
This doc is the **internal architecture** underneath
that identity — how the agent's memory, context loop,
and self-management actually work.

The previous identity (pre-2026-06-08) framed Selena v2
as "a self-contained AI agent with its own memory, heartbeat,
and development loop" — that's still true and is what
this doc covers. The new framing emphasises the
**operational surface** (tracking + transparency + web UI)
over the **agent-internals** story (memory + heartbeat).

## Quick map

| What | How it works | Doc |
|---|---|---|
| **Memory** (5 layers) | File-based hierarchy, no DB | [§ Memory Hierarchy](#memory-hierarchy) below |
| **Context loop** (3 phases) | CONTEXT → CALL → PROCESS, similar to the open-world action loop | [§ Context Loop](#context-loop) below |
| **Web interface** (transparency surface) | The selena-project `:8765` web UI, the #cost-tracker Discord post | [`transparency-and-stats.md`](transparency-and-stats.md) |
| **Self-management** (heartbeat + task priority) | `heartbeat.md` + `priorities.md` + the autonomy policy | `heartbeat.md` (file pointer) |
| **Service watchdog** (two layers) | ① `service_manager` in-process inside selena-api (30s polls, restarts `auto_start: true` services from `docs/projects.md`). ② External `api_health_watchdog.py` (5min timer, restarts selena-api itself, alerts to #selena-project-important). The external watchdog is the safety net for the orchestrator; the in-process watchdog is the fast path for everything else. | [`api-health-watchdog.md`](api-health-watchdog.md) + [`service_manager.py`](../code/service_manager.py) |

## Memory Hierarchy

The 5 layers (top to bottom — most abstract to most concrete):

### 1. Soul Layer (Core Identity)
- **soul.md** - Core identity, values, purpose
- **personality.md** - Traits, communication style
- **guidelines.md** - Operating principles

### 2. Agent Layer (Self-Model)
- **agent.md** - Current state, capabilities
- **skills.md** - Available skills and when to use them
- **tools.md** - Tool descriptions and usage

### 3. Heartbeat Layer (Self-Management)
- **heartbeat.md** - Self-check instructions, priorities
- **health.md** - System status, resource usage
- **goals.md** - Current goals and progress

### 4. Global Memory (Long-term)
- **memory/global/** - Shared knowledge across all sessions
- **memory/daily/** - Daily logs and notes
- **memory/projects/** - Project-specific knowledge
- **memory/reflections/** - Learned insights

### 5. Project Memory (Per-Project)
- **projects/{name}/context/** - Project-specific context
- **projects/{name}/status.md** - Current project status
- **projects/{name}/plans/** - Project plans

## Context Loop

The core agent loop follows the same pattern as Open World world actions:

```
┌─────────────────────────────────────┐
│  1. CONTEXT (Gather)                │
│     - Query relevant memories       │
│     - Load current state            │
│     - Get pending tasks/heartbeats   │
└─────────────────┬───────────────────┘
                  │
                  ▼
┌─────────────────────────────────────┐
│  2. CALL (LLM)                      │
│     - Build prompt from context      │
│     - Make LLM call                 │
│     - Track LLM calls/costs           │
└─────────────────┬───────────────────┘
                  │
                  ▼
┌─────────────────────────────────────┐
│  3. PROCESS (Execute)               │
│     - Parse response                 │
│     - Execute actions                │
│     - Update memory                 │
│     - Schedule follow-up if needed   │
└─────────────────┬───────────────────┘
                  │
                  ▼
         ┌────────┴────────┐
         │ Need more?      │
         │ Loop if yes     │
         └─────────────────┘
```

### Context Phase
- Query global memory for relevant information
- Load project-specific context
- Get current heartbeat tasks
- Gather recent reflections/learnings

### Call Phase
- Build comprehensive prompt
- Include soul/agent context
- Add relevant memories
- Track LLM calls and costs (the 4 main subsystems — see `transparency-and-stats.md`)

### Process Phase
- Parse LLM response
- Execute planned actions
- Update relevant memories
- Determine if more calls needed

## Debug Interface (transparency surface)

The web interface is the canonical transparency surface — what the operator (Arcurus) sees. See [`transparency-and-stats.md`](transparency-and-stats.md) for the full surface map (cost panel, stats panel, todo panel, LLM call panel, pending feedback).

### The 4 main consumer dashboards

| Surface | What it shows | Where it lives |
|---|---|---|
| **Operator view (internal)** | Service health, budget state, autonomy-policy "waiting for your input" queue | Web UI at `:8765` + `orchestrator-status /status` on `:8766` |
| **Arcurus's view (private)** | The same as the operator view, behind auth | Same |
| **Public Discord (external)** | Daily cost report, open-world entity activity, worker reports | `#cost-tracker`, `#openworld`, `#selena-project` channels |
| **Lunar orchestrator (internal)** | The 5h used % + per-project stats for the lunar agents | `orchestrator-status /status` (consumed from selena-project) |

### Web interface

- View current context
- See recent LLM calls
- Monitor memory usage
- View pending tasks

### APIs (the data the surfaces pull from)

- `GET /context` - Current context state
- `GET /memory/search?q=...` - Search memories
- `POST /memory` - Add to memory
- `GET /heartbeat/status` - Heartbeat status
- `POST /heartbeat/task` - Add task
- `GET /llm/calls` - LLM call history (and `/api/openclaw-usage/*` for the full per-call shape)

## Implementation Phases

### Phase 1: Basic Agent Loop
- [x] Core memory system (soul, agent, heartbeat)
- [x] Simple context -> call -> process loop
- [x] Basic file-based storage

### Phase 2: Memory Intelligence
- [x] Relevance-based context retrieval
- [x] Automatic memory prioritization
- [x] Learning from interactions

### Phase 3: Self-Management
- [x] Own heartbeat system
- [x] Priority-based task management
- [x] Resource monitoring

### Phase 4: Debug & Interface (now: Tracking + Transparency + Web UI)
- [x] Web interface (the canonical transparency surface)
- [x] Debug APIs
- [x] Memory visualization
- [x] **Tracking subsystem** (per-LLM-call recording in `data/openclaw_usage.jsonl`)
- [x] **Statistics subsystem** (per-provider / per-model / per-project rollups)
- [x] **Cost panel + #cost-tracker Discord post** (the daily transparency surface)

## Comparison with Open World

| Open World | Selena v2 |
|------------|-----------|
| World Entity | Self (Soul/Agent) |
| World Action | Agent Task |
| Entity Memory | Project Memory |
| Global Context | Global Memory |
| LLM Call | LLM Call |
| Action Processing | Task Processing |

(But the dependency arrow goes the other way too: Selena v2
*tracks* Open World via the LLM-call counter. The world
binary's scheduler posts to selena-project's
`/api/llm-usage/record` on every call. So Selena v2
**owns the transparency surface for Open World**,
not the other way around.)

## Example: Development Loop

```
1. CONTEXT
   - Check heartbeat tasks
   - Load relevant project memories
   - Get recent reflections

2. CALL
   - "Based on pending tasks and recent context, what should I work on next?"
   - Track the call in data/openclaw_usage.jsonl
   - Roll up to per-project / per-provider stats

3. PROCESS
   - Execute task
   - Update project status
   - Add reflection
   - Surface the result on the web UI (transparency)
   - If task incomplete, loop
```

## What this doc is NOT

- **NOT the operator-facing identity.** See
  [`transparency-and-stats.md`](transparency-and-stats.md) for
  "what Selena v2 is for" (tracking + statistics +
  transparency + web UI). This doc is the internals.
- **NOT the cost math.** See
  [`cost-tracking.md`](cost-tracking.md) for the
  per-model pricing math + the `PRICE_PER_1M_USD` table.
- **NOT the LLM-call counter explanation.** See
  [`llm-call-tracking.md`](llm-call-tracking.md) for
  the two-counter legacy + 5h/24h sliding window.

## What this doc replaces

Updated 2026-06-08 per Arcurus: the intro paragraph
previously led with "Selena v2 is a self-contained AI
agent with its own memory, heartbeat, and development
loop" — that's still true and is what the body
covers, but it didn't lead with the new identity
("tracking + statistics + transparency + web UI").
The new intro leads with the new identity and
demotes the memory hierarchy + context loop to
subsections, with a quick-map table at the top so
the reader can navigate to the right detail.

The Phase 4 checkboxes are all checked now (per
2026-06-08 status: web interface, debug APIs,
memory visualization, AND the new tracking/stats/
transparency subsystems are all live). The
"Implementation Phases" section is now a status
report, not a future plan.

---

*Plan created: 2026-04-18; restructured 2026-06-08 per Arcurus #openworld*
