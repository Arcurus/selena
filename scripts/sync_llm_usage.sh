#!/usr/bin/env bash
# Refresh the LLM usage snapshot by running the tracker in CLI mode.
# The tracker queries provider APIs (MiniMax /v1/token_plan/remains) and
# writes data/llm_usage_snapshot.json.  The web UI reads that file.
# Honors per-provider backoff (paused providers are skipped automatically).
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
LOG="/home/openclaw/openclaw/workspace/selena-project/data/llm_usage_sync.log"
echo "[$(date -Iseconds)] sync start" >> "$LOG"
python3 "$ROOT/code/llm_call_tracker.py" sync >> "$LOG" 2>&1
python3 "$ROOT/code/llm_call_tracker.py" status >> "$LOG" 2>&1
echo "[$(date -Iseconds)] sync done" >> "$LOG"
# Trim the log to last 200 lines to avoid unbounded growth
tail -n 200 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
