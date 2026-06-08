# LLM Call Tracking (selena-project)

> **Audience:** operator reference. How the LLM-call counters
> work, why there are two of them, and when each is the source
> of truth. **NOT** loaded in the LLM prompt — the `banker`
> agent enforces the budget; the `cost-tracker` exposes the
> live numbers; the `orchestrator-status` service consumes
> the data.

## Owner: selena-project

The data files live in `selena-project/data/` (NOT in selena-project-2 / lunar). The lunar side (`selena-project-2/orchestrator/orchestrator_status.py` on port 8766) CONSUMES this data via the selena-project API (`/api/llm-usage/*`); it does not own it. The earlier version of this doc was in `selena-project-2/docs/`, which was wrong — fixed 2026-06-08 per Arcurus.

## Two parallel counters

| Counter | File | Format | Started | Source of truth for |
|---|---|---|---|---|
| **Legacy cumulative** | `selena-project/data/llm_calls.json` | JSON object: `{"count": N, "timestamp": "..."}` | April 2026 | Backward-compatible API, the "Cumulative" stat in the web UI |
| **5h / 24h / Today** (sliding window) | `selena-project/data/openclaw_usage.jsonl` | JSONL, one record per call: `{"ts": epoch_ms, "provider": "...", "model": "...", "project": "...", "tokens_in": N, "tokens_out": N, ...}` | June 2026 (replacement path) | The budget gate, the cost tracker, the 5h-window UI |

## Why both

The legacy counter is a single integer incremented on every LLM
call. It has **no timestamps** — it can tell you "we made 15,000
LLM calls total" but not "how many in the last 5 hours". The
sliding-window counter (timestamped records) can answer the 5h
question but didn't exist before June 2026.

**They cannot be merged.** The legacy counter has no timestamps
to backfill. Migrating it would lose all the call history that's
been accumulating since April 2026.

## What is tracked (per LLM call)

Recorded in `data/openclaw_usage.jsonl`, one record per call. The
field shape (per `code/llm_call_tracker.py:record_event`):

- **Per call** — `ts` (ISO 8601), `sessionId`, `kind` (discord/telegram/isolated), `channel`, `cronJobId`, `agentId`, `model`, `provider`, `startedAt`/`updatedAt` (epoch ms), `runtimeMs`, `tokensIn`/`tokensOut`, `cacheRead`/`cacheWrite`, `turnCount`, `estCostUsd`
- **Per provider** — `minimax-portal` / `xai` / `openrouter` (3 providers, 1 default + 2 fallbacks)
- **Per model** — `MiniMax-M3` (default), `xai/grok-4.3` (first fallback), plus the openrouter chain (GLM-5.1, Claude Sonnet 4.6, GPT-5.4, Claude Opus 4.6, Grok-4.20, MiniMax M2.7)
- **Per project** — `open-world-selena`, `selena-project`, `selena-project-lunar`, etc. (each LLM call records the project it was for)
- **Per session** — aggregate tokens / cost / runtime / turns
- **5h sliding window** — the source of truth for the budget gate; the 5h used % is what the `banker` agent and `budget-gate.service` enforce
- **Daily / today / cumulative** — three rollup levels exposed by the cost tracker + the web UI

## How the budget gate uses them

The `banker` agent and the `budget-gate.service` / `.timer`
read the sliding-window JSONL to compute the **5h used %**:

```python
used_pct = (count_in_last_5h / 4500) * 100
```

The gate is **open** when `used_pct < 80` (or `< 50` for
open-world-selena workers — tighter budget because open-world
is the most LLM-hungry project).

The legacy cumulative counter is still updated on every call
(so the "Cumulative" stat in the web UI keeps working) but is
**not** used for any gating decision. It's pure observability.

## When to use which

| Question | Use |
|---|---|
| "How many calls have we made today?" | Sliding window (5h/24h/Today) |
| "How many calls have we made total since April?" | Legacy cumulative |
| "Is the budget gate open?" | Sliding window |
| "How much money have we spent this week?" | Sliding window × per-call cost |
| "Was there a spike in calls at 3 AM last Tuesday?" | Sliding window (per-hour histogram) |

## Who consumes the data

| Consumer | Path | What it reads |
|---|---|---|
| Web UI (`selena-project/web/index.html`) | `/api/cost-tracker`, `/api/openclaw-usage/*` | All counters + rollups |
| Lunar orchestrator-status (port 8766) | `/api/llm-usage/breakdown` | 5h window + per-project |
| `banker` agent | `/api/openclaw-usage/status` | 5h used % (for autonomy decisions) |
| `budget-gate.service` | `/api/openclaw-usage/status` | 5h used % (for the gate) |
| `daily-cost-report` cron | `/api/cost-tracker/report` | All counters (for the #cost-tracker post) |
| `worker-trigger.py` | reads `data/openclaw_usage.jsonl` directly | 5h window (for the trigger's own gate check) |

## Per-model pricing math

Lives in `selena-project/docs/cost-tracking.md` (sibling doc).
The `PRICE_PER_1M_USD` dict in `code/cost_tracker.py` is the
source of truth for the per-model pricing; the rate table
changes (model prices get renegotiated) so it's reference
material, not a behavior-affecting fact.

## What this doc replaces

Originally created 2026-06-08 in `selena-project-2/docs/`
(per the lunar slim-down from MEMORY.md). Moved to
`selena-project/docs/` the same day after Arcurus
flagged: 'llm-usage-sync should be project Selena not
project lunar right? Cumulative LLM call counter
explanation sounds also like project Selena.' The data
files live in `selena-project/data/`, the API endpoints
are in `selena-project/code/api_server.py`, the math is in
`selena-project/code/cost_tracker.py` — none of that is in
selena-project-2. The lunar side is a CONSUMER, not an
owner.
