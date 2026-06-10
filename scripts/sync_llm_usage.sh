#!/usr/bin/bash
# Refresh the LLM usage snapshot.
#
# Steps (added 2026-06-10 per Arcurus #cost-tracker):
#   1. Run `code/refresh_minimax_interval.py` to refresh the
#      `minimax_interval` block from `mmx quota` (the live, working
#      MiniMax token plan endpoint). The previous `python3 code/llm_call_tracker.py
#      sync` was a no-op (see the compat shim's stub docstring), and
#      the snapshot's `providers.minimax.quota` field has been stale
#      since 2026-06-08 because the direct `/v1/token_plan/remains` API
#      call broke. So we now do the refresh ourselves and write the
#      `minimax_interval` block directly.
#   2. Run the tracker in status mode (cheap; reads the snapshot).
#      Kept for backward compat — the optimisation manager reads
#      `llm_usage_snapshot.json` and expects all the legacy fields
#      to still be there.
# Honors per-provider backoff (paused providers are skipped automatically).
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
LOG="/home/openclaw/openclaw/workspace/selena-project/data/llm_usage_sync.log"
echo "[$(date -Iseconds)] sync start" >> "$LOG"
# Step 1: refresh the snapshot's `minimax_interval` block from `mmx quota`.
# Failure is non-fatal: the existing snapshot is left in place.
python3 "$ROOT/code/refresh_minimax_interval.py" >> "$LOG" 2>&1 || true
# Step 2: tracker status (no-op sync + read). Kept for parity with
# earlier runs; cheap.
python3 "$ROOT/code/llm_call_tracker.py" sync >> "$LOG" 2>&1 || true
python3 "$ROOT/code/llm_call_tracker.py" status >> "$LOG" 2>&1 || true
echo "[$(date -Iseconds)] sync done" >> "$LOG"
# Trim the log to last 200 lines to avoid unbounded growth
tail -n 200 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
