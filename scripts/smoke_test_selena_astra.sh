#!/usr/bin/env bash
# smoke_test_selena_astra.sh — quick end-to-end check for the operator dashboard
# ==============================================================================
#
# WHY THIS EXISTS (2026-06-11 per Arcurus #selena-project)
# ----------------------------------------------------------------
# Arcurus reported the /selena-astra/ dashboard "does not load" on 2026-06-11
# 11:34 CET. The page had been fixed once (commit b052dfd, "use relative paths
# so /selena-astra/ works under Caddy prefix") but the regression risk kept
# coming up. Arcurus asked: "dont you check if it still runs if you make
# changes?". This script is the answer: a one-shot smoke test that hits the
# public page + a handful of representative API endpoints and reports
# pass/fail with timing.
#
# Run it:
#   ./scripts/smoke_test_selena_astra.sh                      # default host
#   ./scripts/smoke_test_selena_astra.sh https://example.com  # custom host
#   ./scripts/smoke_test_selena_astra.sh --json               # machine-readable
#
# Exit code:
#   0  every check passed
#   1  at least one check failed
#   2  script misuse (bad args, missing curl)
#
# CHECKS
# ------
#   1. GET  /selena-astra/                 page HTML (200 + non-empty body)
#   2. GET  /selena-astra/static/style.css  CSS asset (200)
#   3. GET  /selena-astra/static/star-script.js  starfield JS (200)
#   4. GET  /selena-astra/api/health       API up (200 + ok:true)
#   5. GET  /selena-astra/api/login?password=...   login flow works (200)
#                                          (only the shape, not the token —
#                                           we throw the token away)
#
# This is intentionally cheap (5 HTTP calls, no JS execution) so it can run
# in under a second. If you need a real browser-driven check, use
# scripts/headless_check.py (or whatever the equivalent becomes).
#
# If a check fails, the script prints a one-line "FAIL: <check> <url> <http>"
# so it can be grep'd from a cron log. The script does NOT post anywhere —
# callers (workers, the slow heartbeat, the watchdog) decide what to do with
# the exit code.

set -uo pipefail

# --- Args --------------------------------------------------------------------

HOST="${SMOKE_HOST:-https://selenaastra.com}"
JSON=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --json)    JSON=1; shift ;;
        --host)    HOST="$2"; shift 2 ;;
        -h|--help)
            sed -n '2,40p' "$0"
            exit 0
            ;;
        http://*|https://*)
            HOST="$1"; shift ;;
        *)
            echo "Unknown arg: $1 (try --help)" >&2
            exit 2
            ;;
    esac
done

# Pre-flight
if ! command -v curl >/dev/null 2>&1; then
    echo "ERROR: curl not found in PATH" >&2
    exit 2
fi

# Load WEB_PASSWORD from .env so check 5 works without an extra env var.
# .env lives next to scripts/..; SELENA_DIR is computed from this script's
# real location (not cwd) so the script is callable from anywhere.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SELENA_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
PASSWORD=""
if [[ -f "$SELENA_DIR/.env" ]]; then
    # Pull the first WEB_PASSWORD=... line, strip optional quotes.
    PASSWORD="$(grep -E '^[[:space:]]*WEB_PASSWORD[[:space:]]*=' "$SELENA_DIR/.env" \
        | head -1 | sed -E 's/^[[:space:]]*WEB_PASSWORD[[:space:]]*=[[:space:]]*//; s/^["'"'"']//; s/["'"'"']$//')"
fi
if [[ -z "$PASSWORD" ]]; then
    echo "ERROR: WEB_PASSWORD not found in $SELENA_DIR/.env" >&2
    exit 2
fi

# --- Check helpers -----------------------------------------------------------
# Each check: sets CHECKS_TOTAL, CHECKS_PASSED, CHECKS_FAIL (arrays of JSON
# lines for --json; arrays of human lines otherwise).

CHECKS_TOTAL=0
CHECKS_PASSED=0
JSON_LINES=()
HUMAN_LINES=()

# run_check <name> <url> <expected_status> [<body_must_contain>]
run_check() {
    local name="$1" url="$2" expect="$3" needle="${4:-}"
    CHECKS_TOTAL=$((CHECKS_TOTAL + 1))

    # Capture body + status in one go. We use two curl temp files so the
    # body and code can't get out of sync, and the code can be 000 on a
    # curl-side failure (e.g. DNS) without confusing the body parser.
    local body_file code_file code body
    body_file="$(mktemp)" || { echo "ERROR: mktemp failed" >&2; exit 2; }
    code_file="$(mktemp)" || { echo "ERROR: mktemp failed" >&2; exit 2; }
    # shellcheck disable=SC2064
    trap "rm -f '$body_file' '$code_file'" RETURN

    curl -sS -o "$body_file" -w '%{http_code}' --max-time 8 "$url" \
        > "$code_file" 2>/dev/null || true
    code="$(cat "$code_file" 2>/dev/null || echo 000)"
    body="$(cat "$body_file" 2>/dev/null || echo '')"

    local ok=0
    if [[ "$code" == "$expect" ]]; then
        if [[ -n "$needle" ]]; then
            [[ "$body" == *"$needle"* ]] && ok=1
        else
            [[ -n "$body" ]] && ok=1
        fi
    fi

    if [[ $ok -eq 1 ]]; then
        CHECKS_PASSED=$((CHECKS_PASSED + 1))
        if [[ $JSON -eq 1 ]]; then
            JSON_LINES+=("{\"check\":\"$name\",\"url\":\"$url\",\"status\":\"pass\",\"http\":$code}")
        else
            HUMAN_LINES+=("  ✓ $name  $url  HTTP $code")
        fi
    else
        if [[ $JSON -eq 1 ]]; then
            JSON_LINES+=("{\"check\":\"$name\",\"url\":\"$url\",\"status\":\"fail\",\"http\":$code}")
        else
            HUMAN_LINES+=("  ✗ $name  $url  HTTP $code (expected $expect)")
        fi
    fi
}

# --- 1-5. Run the checks -----------------------------------------------------

run_check "page"        "$HOST/selena-astra/"                          200 "Selena Astra"
run_check "css"         "$HOST/selena-astra/static/style.css"          200 ""
run_check "stars-js"    "$HOST/selena-astra/static/star-script.js"     200 ""
run_check "api-health"  "$HOST/selena-astra/api/health"                200 '"ok": true'
run_check "api-login"   "$HOST/selena-astra/api/login?password=$PASSWORD" 200 '"success": true'

# --- Report ------------------------------------------------------------------

if [[ $JSON -eq 1 ]]; then
    printf '{"host":"%s","passed":%d,"total":%d}\n' \
        "$HOST" "$CHECKS_PASSED" "$CHECKS_TOTAL"
    for line in "${JSON_LINES[@]}"; do
        printf '%s\n' "$line"
    done
else
    echo "smoke test → $HOST"
    for line in "${HUMAN_LINES[@]}"; do
        printf '%s\n' "$line"
    done
    echo "→ $CHECKS_PASSED/$CHECKS_TOTAL passed"
fi

if [[ $CHECKS_PASSED -eq $CHECKS_TOTAL ]]; then
    exit 0
else
    exit 1
fi
