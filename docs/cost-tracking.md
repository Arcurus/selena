# Cost Tracking — Per-Model Pricing Math (selena-project)

> **Audience:** operator reference. The per-model pricing
> table, the formula, the cache-discount math, and how
> `estCostUsd` is computed for each LLM call. **NOT** loaded
> in the LLM prompt — the numbers change (model prices
> get renegotiated) and the math is reference material.

## Where the data lives

- **Source of truth:** `selena-project/code/cost_tracker.py`
  (Python, the per-call recording + daily report)
- **Mirror:** `code/openclaw_cost_tracker.py` — the same
  `PRICE_PER_1M_USD` table, kept in sync
- **Where the per-call record is stored:** `data/openclaw_usage.jsonl`
  (the sliding-window JSONL)
- **Where the daily report comes from:** `code/cost_tracker.py
  report` (subcommand) or the `/api/cost-tracker/report` endpoint
- **Where the budget gate uses it:** `code/llm_call_tracker.py
  should_proceed` (the banker + budget-gate.service)

## The formula

```python
# cost_tracker.py:compute_event_cost
usd = (tokens_in   / 1_000_000) * price["in"]  \
    + (tokens_out  / 1_000_000) * price["out"] \
    + (cache_read  / 1_000_000) * price["cacheRead"]  \
    + (cache_write / 1_000_000) * price["cacheWrite"]
```

Each model has 4 rates (per 1M tokens, USD). The four buckets
reflect the per-provider pricing model:
- **`in`** — input prompt tokens (highest rate for most providers)
- **`out`** — output completion tokens (often 3-5× the `in` rate)
- **`cacheRead`** — cached prompt tokens that hit a cache (typically
  ~10% of the `in` rate; the provider's "we didn't have to recompute
  this" discount)
- **`cacheWrite`** — tokens that get written to the cache (often
  billed at the `in` rate, with Anthropic charging a small
  premium; some providers don't have this and the rate is 0)

## The per-model table

(As of 2026-06-08; **ratings are USD per 1M tokens**. Sourced from
`selena-project/code/cost_tracker.py:PRICE_PER_1M_USD`. The dict
in the code is the source of truth — this table is regenerated
from the code, not the other way around.)

| Model | in | out | cacheRead | cacheWrite | Provider |
|---|---:|---:|---:|---:|---|
| `MiniMax-M3` | 0.50 | 1.00 | 0.05 | 0.50 | minimax-portal |
| `MiniMax-M2.7-highspeed` | 0.30 | 0.60 | 0.03 | 0.30 | minimax-portal |
| `MiniMax-M2.7` | 0.30 | 0.60 | 0.03 | 0.30 | minimax-portal |
| `grok-4.3` | 3.00 | 15.00 | 0.30 | 3.75 | xai |
| `grok-4.3-fast` | 0.20 | 0.50 | 0.02 | 0.20 | xai |
| `grok-4.1-fast` | 0.20 | 0.50 | 0.02 | 0.20 | xai |
| `anthropic/claude-sonnet-4.5` | 3.00 | 15.00 | 0.30 | 3.75 | openrouter |
| `anthropic/claude-sonnet-4.6` | 3.00 | 15.00 | 0.30 | 3.75 | openrouter |
| `anthropic/claude-opus-4.6` | 15.00 | 75.00 | 1.50 | 18.75 | openrouter |
| `openai/gpt-4o` | 2.50 | 10.00 | 0.25 | 2.50 | openrouter |
| `openai/gpt-4o-mini` | 0.15 | 0.60 | 0.015 | 0.15 | openrouter |
| `openai/gpt-5.4` | 5.00 | 20.00 | 0.50 | 5.00 | openrouter |
| `openai/gpt-5.2-codex` | 5.00 | 20.00 | 0.50 | 5.00 | openrouter |

## Models NOT in the table

If a model doesn't have an entry in `PRICE_PER_1M_USD`, the cost
tracker:
- Logs a warning ("model X not in price table; cost = 0 for this call")
- Records `estCostUsd = 0` for that call
- The LLM call IS still tracked (token counts, runtime, etc.) — only
  the cost estimate is missing
- The daily report shows these as "(unpriced)" so operators notice

This is defensive — better to undercount cost than to crash the
report when a new model ships before the price table is updated.

## The "default model" implication

The default model is `minimax-portal/MiniMax-M3` ($0.50/$1.00).
A typical action cycle:
- 5,000 input tokens
- 500 output tokens
- 200,000 cached (the per-entity context is mostly cache hits)
- 1,000 cacheWrite (the entity template gets re-cached)

= (5_000/1M) * 0.50  +  (500/1M) * 1.00  +  (200_000/1M) * 0.05  +  (1_000/1M) * 0.50
= 0.0025 + 0.0005 + 0.0100 + 0.0005
= **$0.0135 per action cycle**

So a 5h budget of 4500 calls × ~$0.01 = ~$45 max (assuming all
default-model calls). Actual spend is much lower because
most calls use cache reads (the $0.0135 above is generous).

## How the cache discount works in practice

For the open-world action cycles, the per-call breakdown is
typically:
- `in`: 5-20k tokens (the per-entity context, properties, nearby
  entities, history)
- `out`: 1-3k tokens (the LLM's response, includes the action +
  outcome + narrative + effects block)
- `cacheRead`: 100-500k tokens (most of the system prompt, the
  property catalog, the world-mechanics doc — these get cached
  after the first call)
- `cacheWrite`: 0-50k tokens (the system prompt refreshes
  occasionally; the property catalog is bigger than 8kB so
  it gets cache-written)

So the cache discount can make a real difference: a 200k
cacheRead at $0.05/M = $0.01, vs the same 200k at the `in` rate
of $0.50/M = $0.10. **10× cheaper.**

This is why the LLM-usage JSONL tracks cacheRead/cacheWrite
separately — operators need to see the cache hit rate to
understand actual spend.

## Aggregate rollups

The cost tracker exposes 3 rollup levels:

| Rollup | Where it shows up | Use case |
|---|---|---|
| Per-call | `data/openclaw_usage.jsonl` (each record has `estCostUsd`) | Forensic — "what did that one call cost?" |
| Per-session | `data/llm_calls.json` (the legacy cumulative) | "How much did the worker's last run cost?" |
| Per-day / per-5h | `/api/cost-tracker` JSON, the daily #cost-tracker post | "What's the burn rate this week?" |

The `banker` agent and `budget-gate.service` use the 5h
per-day rollup to compute `used_pct = (calls_5h / 4500) * 100`
for the budget gate. The legacy `data/llm_calls.json` is
referenced by the "Cumulative" stat in the web UI but not
used for any gating decision.

## What this doc replaces

Added 2026-06-08 per Arcurus: *"the cost tracker is
important, at least it must stay in memory how to track
costs and what costs are tracked. the rest of the
tracking can then move to a linked doc."* MEMORY.md
keeps the "what is tracked" (the per-call field shape,
the rollup levels) — this doc holds the "how" (the
formula) and the "how much" (the price table).

The `PRICE_PER_1M_USD` dict in the code is the source of
truth. When you change a price, change the code first,
then regenerate this table from a script (or run
`python3 code/cost_tracker.py prices` to print the live
values).
