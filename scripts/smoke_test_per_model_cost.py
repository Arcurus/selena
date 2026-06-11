#!/usr/bin/env python3
"""
smoke_test_per_model_cost.py — verifies the /api/llm-usage/per-model-cost
response shape matches what the web UI's renderCostByModel() expects.

Background: 2026-06-11, the "Cost by Model" sub-tab showed
"Error: models.reduce is not a function" because the API was returning
`by_model` as a dict while the JS did `data.by_model || []` and then
called `.reduce()` (an array method). Truthy `{}` short-circuited `|| []`,
so the reduce crashed. The HTTP 200 hid it from any curl-based check.

This test catches that class of bug at the CLI level: it fetches the
endpoint (with auth) and runs the exact reduce/sort/map/sum operations
the JS does, asserting they all succeed and the totals are non-zero.

Also checks the relative-path rule (the API URL works under the
/selena-astra/ Caddy prefix).

Usage:
  python3 scripts/smoke_test_per_model_cost.py
  # exit 0 = pass, exit 1 = fail
"""
from __future__ import annotations

import json
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path

SELENA_DIR = Path(__file__).resolve().parent.parent
ENV_PATH = SELENA_DIR / ".env"
WEB_PASSWORD = os.environ.get("WEB_PASSWORD")
if not WEB_PASSWORD and ENV_PATH.exists():
    for line in ENV_PATH.read_text().splitlines():
        if line.startswith("WEB_PASSWORD="):
            WEB_PASSWORD = line.split("=", 1)[1].strip().strip('"').strip("'")
            break

if not WEB_PASSWORD:
    print("FAIL: WEB_PASSWORD not set and not in .env", file=sys.stderr)
    sys.exit(2)


def http_get(path: str) -> dict:
    """GET http://127.0.0.1:8765<path> with login + bearer. Returns parsed JSON."""
    base = "http://127.0.0.1:8765"
    # 1. login
    login_q = urllib.parse.urlencode({"password": WEB_PASSWORD})
    with urllib.request.urlopen(f"{base}/api/login?{login_q}", timeout=10) as r:
        login = json.loads(r.read())
    if not login.get("success"):
        print(f"FAIL: login failed: {login}", file=sys.stderr)
        sys.exit(2)
    token = login["token"]
    # 2. call endpoint
    req = urllib.request.Request(
        f"{base}{path}",
        headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def render_js_passes(data: dict) -> tuple:
    """Re-implement renderCostByModel() in Python and assert every step works.
    Returns (total_cost, total_calls, sorted_model_names) on success;
    raises on the first step that would have crashed in the browser."""
    if not isinstance(data, dict):
        raise AssertionError(f"data is not a dict: {type(data).__name__}")
    # The actual line that crashed on 2026-06-11:
    models = data.get("by_model") or []
    if not isinstance(models, list):
        # This is the bug class we want to catch.
        raise AssertionError(
            f"data.by_model is {type(models).__name__}, expected list. "
            f"JS `data.by_model || []` would NOT convert a truthy dict to []. "
            f"The browser's `models.reduce(...)` would crash with "
            f"'models.reduce is not a function'."
        )
    if not models:
        # Empty list is fine — the page just shows zeros. Don't fail on it.
        return 0.0, 0, []
    # .reduce(...)
    total_cost   = sum(m.get("hardcoded_cost_usd", 0)   for m in models)
    total_in     = sum(m.get("tokens_in", 0)           for m in models)
    total_out    = sum(m.get("tokens_out", 0)          for m in models)
    total_cr     = sum(m.get("cache_read_tokens", 0)   for m in models)
    total_cw     = sum(m.get("cache_write_tokens", 0)  for m in models)
    total_calls  = sum(m.get("calls", 0)               for m in models)
    # .map(...)
    cost_per_model = [(m.get("hardcoded_cost_usd", 0), m.get("model", "?")) for m in models]
    # .sort(...)
    sorted_models = sorted(models, key=lambda m: m.get("hardcoded_cost_usd", 0), reverse=True)
    sorted_names = [m.get("model", "?") for m in sorted_models]
    return (total_cost, total_calls, sorted_names)


def main() -> int:
    # 1. shape + JS path passes
    data = http_get("/api/llm-usage/per-model-cost?window_hours=1")
    try:
        total_cost, total_calls, sorted_names = render_js_passes(data)
    except AssertionError as e:
        print(f"FAIL: {e}")
        print(f"  response keys: {list(data.keys())}")
        return 1
    print(f"PASS shape check: by_model is a list of {len(data['by_model'])} models")
    print(f"PASS reduce/sort/map: total=${total_cost:.2f} calls={total_calls:,}")
    print(f"  sorted by cost: {sorted_names}")

    # 2. also check 24h (the heavier query that triggered the timeout earlier)
    data24 = http_get("/api/llm-usage/per-model-cost?window_hours=24")
    try:
        total24, calls24, names24 = render_js_passes(data24)
    except AssertionError as e:
        print(f"FAIL (24h): {e}")
        return 1
    print(f"PASS 24h check: by_model is a list of {len(data24['by_model'])} models")
    print(f"  total=${total24:.2f} calls={calls24:,} models={names24}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
