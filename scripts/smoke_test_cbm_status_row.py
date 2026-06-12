#!/usr/bin/env python3
"""
Smoke test for the Cost by Model status row (last/reset/countdown).

Per Arcurus 2026-06-12 12:03 CEST #cost-tracker: 'display there also
the minutes when the reset happens and which time it will be. same
as for the top right display. and please display also the last
update, and update the display similar'.

The new status row mirrors the top-right MiniMax 5h widget format:
  last HH:MM:SS    - when the cost data was last fetched
  reset HH:MM      - MiniMax 5h window reset (from the same source
                     as the top-right widget, so the two displays
                     stay in lockstep)
  \u21bb live count   - ticks every second

This test verifies:

  1. The HTML has the 3 new elements (cbmStatusLast, cbmStatusReset,
     cbmStatusNext) inside the cbmStatusRow container
  2. The status row is positioned AFTER the time-range selector but
     BEFORE the KPIs (so it reads as a status bar for the panel)
  3. _cbmUpdateStatusRow() function exists
  4. _cbmTickStatusRow() function exists
  5. _cbmStartStatusTick() function exists + uses setInterval(..., 1000)
  6. loadCostByModel() calls _cbmUpdateStatusRow + _cbmStartStatusTick
  7. The tick function reads from the global minimaxResetAt (so it
     stays in sync with the top-right widget)
  8. The status row uses the same CSS classes (.minimax-row-bottom,
     .minimax-meta) as the top-right widget for visual consistency
"""
import json
import re
import sys
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "web" / "index.html"


def _find_function_body(src: str, name: str) -> str:
    """Brace-balanced slice of a top-level function body."""
    idx = src.find(f"function {name}(")
    if idx < 0:
        return ""
    open_b = src.find("{", idx)
    if open_b < 0:
        return ""
    depth = 1
    i = open_b + 1
    while i < len(src) and depth > 0:
        if src[i] == "{": depth += 1
        elif src[i] == "}": depth -= 1
        i += 1
    return src[open_b:open_b + (i - open_b)]


def main() -> int:
    src = WEB.read_text()

    # 1. The 3 new elements exist
    for el_id in ("cbmStatusRow", "cbmStatusLast", "cbmStatusReset", "cbmStatusNext"):
        if f'id="{el_id}"' not in src:
            print(f"FAIL: missing element id='{el_id}' in web/index.html")
            return 1
    print("OK status row has all 4 elements (container + last + reset + next)")

    # 2. Status row positioned AFTER the window buttons but BEFORE the KPIs.
    # Use the last cbm-window-btn BEFORE the KPIs div (not src.rfind,
    # which would land in the Detail table's second selector that comes
    # AFTER the KPIs in the source).
    kpis_div = src.find('id="cbmKpis"')
    status_row = src.find('id="cbmStatusRow"')
    # Find the last window button in the slice before the KPIs div.
    last_btn = src.rfind("cbm-window-btn", 0, kpis_div)
    if not (last_btn < status_row < kpis_div):
        print(f"FAIL: status row is not between the window buttons and KPIs")
        print(f"      last_btn@{last_btn} status_row@{status_row} kpis@{kpis_div}")
        return 1
    print("OK status row is positioned between the window selector and the KPIs")

    # 3-5. Helper functions exist
    for fn in ("_cbmUpdateStatusRow", "_cbmTickStatusRow", "_cbmStartStatusTick"):
        if f"function {fn}(" not in src:
            print(f"FAIL: function {fn} not defined")
            return 1
    print("OK all 3 helper functions exist (_cbmUpdateStatusRow, _cbmTickStatusRow, _cbmStartStatusTick)")

    # 5b. _cbmStartStatusTick uses setInterval(..., 1000) (1-second tick)
    body = _find_function_body(src, "_cbmStartStatusTick")
    if "setInterval(" not in body or ", 1000)" not in body:
        print(f"FAIL: _cbmStartStatusTick doesn't use setInterval(fn, 1000)")
        return 1
    print("OK _cbmStartStatusTick uses setInterval(..., 1000) — 1-second tick")

    # 6. loadCostByModel calls the helpers
    body = _find_function_body(src, "loadCostByModel")
    if "_cbmUpdateStatusRow(data)" not in body:
        print("FAIL: loadCostByModel doesn't call _cbmUpdateStatusRow(data)")
        return 1
    if "_cbmStartStatusTick()" not in body:
        print("FAIL: loadCostByModel doesn't call _cbmStartStatusTick()")
        return 1
    print("OK loadCostByModel calls _cbmUpdateStatusRow + _cbmStartStatusTick")

    # 7. _cbmTickStatusRow reads from global minimaxResetAt
    tick_body = _find_function_body(src, "_cbmTickStatusRow")
    if "minimaxResetAt" not in tick_body:
        print("FAIL: _cbmTickStatusRow doesn't reference minimaxResetAt")
        print("      (must read from the same global as the top-right widget for in-sync ticking)")
        return 1
    if "minimaxFormatCountdown" not in tick_body:
        print("FAIL: _cbmTickStatusRow doesn't use minimaxFormatCountdown for the '↻ 1h 10m' format")
        return 1
    print("OK _cbmTickStatusRow reads from minimaxResetAt (synced with top-right widget)")

    # 7b. _cbmUpdateStatusRow also reads minimaxResetAt
    upd_body = _find_function_body(src, "_cbmUpdateStatusRow")
    if "minimaxResetAt" not in upd_body:
        print("FAIL: _cbmUpdateStatusRow doesn't reference minimaxResetAt")
        return 1
    if "minimaxFormatResetTime" not in upd_body:
        print("FAIL: _cbmUpdateStatusRow doesn't use minimaxFormatResetTime for HH:MM format")
        return 1
    if "minimaxFormatClockTime" not in upd_body:
        print("FAIL: _cbmUpdateStatusRow doesn't use minimaxFormatClockTime for last-fetched HH:MM:SS")
        return 1
    if "data.at" not in upd_body:
        print("FAIL: _cbmUpdateStatusRow doesn't read data.at for the last-update time")
        return 1
    print("OK _cbmUpdateStatusRow formats data.at + minimaxResetAt correctly")

    # 8. CSS class consistency with top-right widget
    status_row_idx = src.find('id="cbmStatusRow"')
    # Walk back to find the start of the <div> tag
    div_start = src.rfind("<div", 0, status_row_idx)
    div_end = src.find(">", div_start)
    opening_tag = src[div_start:div_end + 1]
    if 'minimax-row-bottom' not in opening_tag:
        print(f"FAIL: status row opening tag missing 'minimax-row-bottom' class")
        print(f"      got: {opening_tag[:200]}")
        return 1
    # Check the inner spans use .minimax-meta
    inner_slice = src[div_start:src.find("</div>", div_start) + 6]
    if 'class="minimax-meta"' not in inner_slice:
        print("FAIL: status row inner spans missing 'minimax-meta' class")
        return 1
    print("OK status row uses .minimax-row-bottom + .minimax-meta classes (matches top-right widget)")

    # 9. AGENTS.md relative-path discipline
    abs_fetch = re.findall(r"fetch\(['\"]/", src)
    if abs_fetch:
        print(f"FAIL: {len(abs_fetch)} absolute-path fetch() in web/index.html")
        return 1
    print("OK no absolute-path fetch() in web/index.html")

    print()
    print("ALL CHECKS PASSED ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
