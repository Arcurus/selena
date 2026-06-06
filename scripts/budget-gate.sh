#!/usr/bin/env bash
# budget-gate.sh — gate a command behind the LLM budget check.
#
# Per Arcurus (2026-06-03):
#   - "postpone autonomous or resource intensive tasks until the next refresh"
#   - "make the wait command the default if close to budget end"
#
# Behaviour (refined 2026-06-03 13:00):
#   1. Cheap pre-check via llm_call_tracker.py
#   2. If budget OK → run the command
#   3. If budget tight but recoverable within --max-wait-s → WAIT, then run
#   4. If budget hopeless (no reset within --max-wait-s) → defer (exit 1)
#
# Usage:
#   budget-gate.sh [--project P] [--additional N] [--max-wait-s S] [--quiet] -- command args...
#
# Exit codes:
#   0   budget OK (or waited + recovered), command ran
#   1   budget tight, command was NOT run (after waiting up to --max-wait-s)
#   2   bad usage
#
# Examples:
#   # Strict: fail if budget tight (no waiting)
#   budget-gate.sh --max-wait-s 0 --project open-world-selena --additional 20 -- ./refactor.sh
#
#   # Default: wait up to 30 min for the 5h window to reset
#   budget-gate.sh --project open-world-selena --additional 50 -- ./refactor.sh
#
#   # Long-wait: wait up to 4h (e.g. for a not-urgent nightly job)
#   budget-gate.sh --max-wait-s 14400 --project selena --additional 30 -- ./nightly.sh
#
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
TRACKER="python3 $ROOT/code/llm_call_tracker.py"

PROJECT=""
ADDITIONAL=1
MAX_WAIT_S=1800        # default: wait up to 30 min
QUIET=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --project)    PROJECT="--project $2"; shift 2;;
        --additional) ADDITIONAL="$2"; shift 2;;
        --max-wait-s) MAX_WAIT_S="$2"; shift 2;;
        --quiet|-q)   QUIET=1; shift;;
        --) shift; break;;
        -h|--help)
            sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'
            exit 0;;
        *) echo "budget-gate: unknown arg: $1" >&2; exit 2;;
    esac
done
[[ $# -lt 1 ]] && { echo "budget-gate: missing command after --" >&2; exit 2; }

# Helper: parse a JSON reason out of a JSON object
_json_reason() { python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('reason','?'))" 2>/dev/null || echo "unknown"; }
_json_proceed() { python3 -c "import json,sys; d=json.load(sys.stdin); print('true' if d.get('proceed') else 'false')" 2>/dev/null || echo "false"; }
_json_seconds_to_reset() { python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('seconds_until_window_resets', 0) or 0)" 2>/dev/null || echo "0"; }

# 1) Cheap pre-check
CHECK_JSON=$($TRACKER check $PROJECT --additional "$ADDITIONAL" 2>&1) || true
PROCEED=$(echo "$CHECK_JSON" | _json_proceed)
SECONDS_TO_RESET=$(echo "$CHECK_JSON" | _json_seconds_to_reset)

if [[ "$PROCEED" == "true" ]]; then
    [[ $QUIET -eq 0 ]] && echo "✓ budget ok, running: $*" >&2
    exec "$@"
fi

# 2) Budget tight.  Decide: wait, or defer?
REASON=$(echo "$CHECK_JSON" | _json_reason)
if [[ "$MAX_WAIT_S" -le 0 ]] || [[ "$SECONDS_TO_RESET" -gt "$MAX_WAIT_S" ]]; then
    # Defer
    [[ $QUIET -eq 0 ]] && echo "⏸ budget tight, deferring: $REASON (would need ${SECONDS_TO_RESET}s, max ${MAX_WAIT_S}s)" >&2
    exit 1
fi

# 3) Wait.  The tracker's `wait` command blocks up to --max-wait-s.
[[ $QUIET -eq 0 ]] && echo "⏳ budget tight, waiting up to ${MAX_WAIT_S}s for reset: $REASON" >&2
WAIT_JSON=$($TRACKER wait $PROJECT --additional "$ADDITIONAL" --max-wait-s "$MAX_WAIT_S" 2>&1) || true
WAIT_PROCEED=$(echo "$WAIT_JSON" | _json_proceed)
WAIT_REASON=$(echo "$WAIT_JSON" | _json_reason)

if [[ "$WAIT_PROCEED" == "true" ]]; then
    [[ $QUIET -eq 0 ]] && echo "✓ budget recovered, running: $*" >&2
    exec "$@"
fi

# 4) Still tight after waiting.  Defer.
[[ $QUIET -eq 0 ]] && echo "⏸ budget still tight after waiting: $WAIT_REASON" >&2
exit 1
