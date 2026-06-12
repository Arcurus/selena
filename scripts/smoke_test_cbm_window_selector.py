#!/usr/bin/env python3
"""
Smoke test for the Cost by Model time-range selector.

Per Arcurus 2026-06-12 10:47 CEST #cost-tracker: "in the Detailed
table please make costs selectable by 1h, 24h, 7days, 1 month,
year, all". This test verifies:

  1. The /api/llm-usage/per-model-cost endpoint respects
     window_hours (1, 24, 168, 720, 8760, 0=all)
  2. window_hours=0 returns MORE events than any positive value
     (i.e. "all-time" is the union of everything)
  3. The response shape is identical across windows
  4. web/index.html has 6 window buttons with the right data-hours
     values and an onclick that calls setCostByModelWindow
  5. setCostByModelWindow() function exists and updates the
     _cbmWindowHours state + re-fetches

This catches the class of bug: silently dropping the window_hours
param, ignoring window_hours=0, or rendering the wrong button as
active.
"""
import json
import re
import sys
import urllib.request
from pathlib import Path
from urllib.parse import urlencode

ENV = Path(__file__).resolve().parent.parent / ".env"
API_BASE = "http://localhost:8765"
WEB = Path(__file__).resolve().parent.parent / "web" / "index.html"

# Expected windows: (label, data-hours attr, server window_hours int)
EXPECTED_WINDOWS = [
    ("1h",  "1",    1),
    ("24h", "24",   24),
    ("7d",  "168",  168),
    ("1mo", "720",  720),
    ("1y",  "8760", 8760),
    ("all", "0",    0),
]


def get_password() -> str:
    for line in ENV.read_text().splitlines():
        if line.startswith("WEB_PASSWORD="):
            return line.split("=", 1)[1].strip()
    raise SystemExit("WEB_PASSWORD not found in .env")


def login() -> str:
    pw = get_password()
    with urllib.request.urlopen(f"{API_BASE}/api/login?{urlencode({'password': pw})}") as r:
        body = json.loads(r.read())
    if not body.get("success"):
        raise SystemExit(f"Login failed: {body}")
    return body["token"]


def fetch(token: str, params: dict) -> dict:
    qs = urlencode(params)
    req = urllib.request.Request(
        f"{API_BASE}/api/llm-usage/per-model-cost?{qs}",
        headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())


def main() -> int:
    token = login()
    src = WEB.read_text()

    # 1. Endpoint respects window_hours
    events_by_window = {}
    for label, _dh, wh in EXPECTED_WINDOWS:
        data = fetch(token, {"window_hours": wh})
        win_h = data.get("window_hours")
        ev = data.get("events_count")
        n_models = len(data.get("by_model") or [])
        if wh == 0:
            # Server returns 0.0 for "all" which is the sentinel
            assert win_h == 0, f"all: expected window_hours=0, got {win_h}"
        else:
            assert win_h == wh, f"{label}: expected window_hours={wh}, got {win_h}"
        events_by_window[wh] = ev
        print(f"OK {label:<5} window_hours={wh:>4}  events={ev:>6,}  models={n_models}")

    # 2. The "all" window has >= events of any positive window
    all_events = events_by_window[0]
    for wh, ev in events_by_window.items():
        if wh == 0:
            continue
        assert ev <= all_events, (
            f"window_hours={wh}: events={ev} > all-time events={all_events} "
            f"(narrowing broken — wider window should see more events)"
        )
    print(f"OK all positive windows (1h..1y) are subsets of all-time ({all_events:,} events)")

    # 2b. Wider windows see more events (sanity: 1h <= 24h <= 7d <= 1mo <= 1y)
    prev = 0
    for wh in (1, 24, 168, 720, 8760):
        ev = events_by_window[wh]
        assert ev >= prev, f"window_hours={wh} has {ev} events < prev {prev} (windows not monotonically widening)"
        prev = ev
    print(f"OK events monotonically increase with wider window ({events_by_window[1]} → {events_by_window[8760]:,})")

    # 3. Response shape identical across windows
    for label, dh, wh in EXPECTED_WINDOWS:
        data = fetch(token, {"window_hours": wh})
        for k in ("by_model", "missing_prices", "drift_flags", "at",
                  "window_hours", "events_count", "drift_threshold_pct"):
            if k not in data:
                print(f"FAIL: {label}: response missing key '{k}'")
                return 1
        for m in data.get("by_model") or []:
            for mk in ("model", "known", "calls", "tokens_in", "tokens_out",
                       "cache_read_tokens", "cache_write_tokens",
                       "hardcoded_cost_usd", "drift_pct",
                       "price_in_usd_per_1m", "price_out_usd_per_1m"):
                if mk not in m:
                    print(f"FAIL: {label}: model entry missing '{mk}' (model={m.get('model')})")
                    return 1
    print(f"OK response shape consistent across all 6 windows")

    # 4. Cache read is non-zero in 24h (regression for the bug)
    data_24 = fetch(token, {"window_hours": 24})
    has_cr = any((m.get("cache_read_tokens") or 0) > 0
                 for m in (data_24.get("by_model") or []))
    if not has_cr:
        print("FAIL: 24h window has zero cache_read tokens (cache_read bug regressed)")
        return 1
    print("OK 24h window has non-zero cache_read tokens (regression check)")

    # 5. HTML has 6 window buttons PER selector (top of sub-tab + top of
    # Detail table = 12 total). Per Arcurus 2026-06-12 12:00 CEST
    # #cost-tracker: "display also in top of Detail table. it can still
    # affect also the current selector if you press there."
    btn_pattern = re.compile(
        r'<button[^>]*class="save-btn cbm-window-btn"[^>]*data-hours="(\d+)"[^>]*onclick="setCostByModelWindow\(([^,]+),[^"]+\)"[^>]*>([^<]+)</button>'
    )
    btns = btn_pattern.findall(src)
    if len(btns) != 12:
        print(f"FAIL: expected 12 window buttons (6 top + 6 Detail table), found {len(btns)}")
        return 1
    print(f"OK 12 window buttons present in web/index.html (6 top + 6 Detail table)")

    # 5b. The 12 buttons cover exactly the 6 expected (label, hours) pairs,
    # each appearing twice (once in each selector).
    actual_btns = [(label.strip(), dh) for dh, _arg, label in btns]
    from collections import Counter
    pair_counts = Counter(actual_btns)
    expected_pairs = [(label, dh) for label, dh, _wh in EXPECTED_WINDOWS]
    for pair in expected_pairs:
        if pair_counts.get(pair, 0) != 2:
            print(f"FAIL: (label, hours) pair {pair} appears {pair_counts.get(pair, 0)} times, expected 2 (top + Detail table)")
            return 1
    print(f"OK all 6 (label, hours) pairs appear exactly twice (top + Detail table)")

    # 5c. The second selector is INSIDE the Detail table panel (between
    # the <h3>🔍 Detail table</h3> heading and the <div id="cbmTable">).
    m = re.search(r'<h3>[^<]*Detail table</h3>', src)
    if not m:
        print("FAIL: couldn't find the Detail table heading")
        return 1
    detail_panel_start = m.start()
    detail_table_div = src.find('id="cbmTable"', detail_panel_start)
    if detail_panel_start < 0 or detail_table_div < 0:
        print("FAIL: couldn't locate the Detail table panel markers")
        return 1
    detail_panel = src[detail_panel_start:detail_table_div]
    if 'class="save-btn cbm-window-btn"' not in detail_panel:
        print("FAIL: second selector is NOT inside the Detail table panel")
        return 1
    if 'setCostByModelWindow' not in detail_panel:
        print("FAIL: second selector doesn't use setCostByModelWindow")
        return 1
    print("OK second selector is inside the Detail table panel and uses setCostByModelWindow (stays in sync with top selector)")

    # 6. Each button has the expected data-hours, function arg, and label
    for (expected_label, expected_dh, expected_wh), (got_dh, got_arg, got_label) in zip(EXPECTED_WINDOWS, btns):
        assert got_dh == expected_dh, f"button {got_label}: data-hours={got_dh} expected {expected_dh}"
        assert got_arg.strip() == expected_dh, f"button {got_label}: arg={got_arg} expected {expected_dh}"
        assert got_label.strip() == expected_label, \
            f"button {got_label}: label mismatch (expected {expected_label})"
    print("OK all 6 buttons: data-hours, onclick arg, and label match the spec")

    # 7. setCostByModelWindow function exists and wires to _cbmWindowHours
    if "function setCostByModelWindow" not in src:
        print("FAIL: setCostByModelWindow function not defined in web/index.html")
        return 1
    if "_cbmWindowHours" not in src:
        print("FAIL: _cbmWindowHours state variable not defined")
        return 1
    # Slice the setCostByModelWindow function body to verify it
    # calls loadCostByModel(false) to re-fetch.
    sm_start = src.find("function setCostByModelWindow")
    if sm_start < 0:
        print("FAIL: couldn't find setCostByModelWindow start")
        return 1
    open_b = src.find("{", sm_start)
    depth = 1
    i = open_b + 1
    while i < len(src) and depth > 0:
        if src[i] == "{": depth += 1
        elif src[i] == "}": depth -= 1
        i += 1
    sm_body = src[open_b:open_b + (i - open_b)]
    if "loadCostByModel(false)" not in sm_body:
        print("FAIL: setCostByModelWindow doesn't call loadCostByModel(false) to re-fetch")
        return 1
    print("OK setCostByModelWindow exists + re-fetches via loadCostByModel(false)")

    # 8. loadCostByModel passes window_hours to the API
    m = re.search(r"async function loadCostByModel\([^)]*\)\s*\{", src)
    if not m:
        print("FAIL: couldn't find loadCostByModel")
        return 1
    # Brace-balanced slice
    open_b = src.find("{", m.end() - 1)
    depth = 1
    i = open_b + 1
    while i < len(src) and depth > 0:
        if src[i] == "{": depth += 1
        elif src[i] == "}": depth -= 1
        i += 1
    body = src[open_b:open_b + (i - open_b)]
    if "window_hours=" not in body:
        print("FAIL: loadCostByModel doesn't append window_hours to the API URL")
        return 1
    print("OK loadCostByModel sends window_hours to the API")

    # 9. AGENTS.md relative-path discipline
    abs_fetch = re.findall(r"fetch\(['\"]/", src)
    if abs_fetch:
        print(f"FAIL: {len(abs_fetch)} absolute-path fetch() in web/index.html")
        return 1
    print("OK no absolute-path fetch() in web/index.html")

    print()
    print("ALL CHECKS PASSED ✅")
    return 0


def _label_for_dh(dh: str) -> str:
    return {"1": "1h", "24": "24h", "168": "7d", "720": "1mo", "8760": "1y", "0": "all"}[dh]


if __name__ == "__main__":
    sys.exit(main())
