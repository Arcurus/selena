#!/usr/bin/env bash
# Run the OpenClaw-usage reconciler to capture cron agentTurn calls
# (selena-project-worker, slow-heartbeat, lunar-worker, etc.) into
# data/llm_usage_events.jsonl. The cron jobs run through OpenClaw's
# gateway (localhost:18789/v1/chat/completions) and are otherwise
# invisible to cost_tracker.py / llm_call_tracker.py.
#
# The reconciler:
#   1. Reads `openclaw status --usage --json`
#   2. For each session with non-null totalTokens that we have not
#      yet recorded, appends an event row with project=openclaw-direct
#   3. Dedups by sessionId (state file: data/reconcile_openclaw_state.json)
#
# See todo f1a2b3c4-OPENCLAW-REC and code/reconcile_openclaw_usage.py.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
LOG="/home/openclaw/openclaw/workspace/selena-project/data/reconcile_openclaw.log"
echo "[$(date -Iseconds)] reconcile start" >> "$LOG"
python3 "$ROOT/code/reconcile_openclaw_usage.py" poll >> "$LOG" 2>&1 || \
  echo "[$(date -Iseconds)] reconcile exited non-zero (see log above)" >> "$LOG"
echo "[$(date -Iseconds)] reconcile done" >> "$LOG"
# Trim the log to last 200 lines to avoid unbounded growth
tail -n 200 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
