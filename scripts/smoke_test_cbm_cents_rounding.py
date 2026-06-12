#!/usr/bin/env python3
"""
Smoke test for the cents-rounding fix in the Cost by Model sub-tab.

Per Arcurus 2026-06-12 14:10 CEST #cost-tracker: 'can you round
down the costs to cents? like 0.13 instead of 0.1343'.

The fix:
  - _fmtCost() in web/index.html now always rounds DOWN (floor) to
    2 decimal places. Previous behavior used 3-4 decimals for tiny
    costs which was noisy and not what the user wanted.
  - All 4 cost display locations in renderCostByModel now use
    _fmtCost() instead of raw .toFixed() calls:
      * KPI total (line ~6510)
      * Bar chart value label (line ~6549)
      * $ cost list under the chart (line ~6557)
      * Detail table cost column (line ~6664)

This test asserts:
  1. The 4 cost display locations in renderCostByModel use
    _fmtCost() (not raw .toFixed(4) for cost values)
  2. _fmtCost() rounds DOWN to 2 decimals (re-implement the JS
    in Python and verify on the canonical cases)
  3. The totalCost KPI also uses _fmtCost() (so 0.1350 -> $0.13,
    not $0.14 from a half-up round)
  4. AGENTS.md relative-path discipline
"""
import re
import sys
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "web" / "index.html"


def _fmt_cost_py(usd: float | None) -> str:
    """Python re-impl of the JS _fmtCost. Must match exactly."""
    if usd is None:
        return "$0"
    # Math.floor(usd * 100) / 100 in Python: int(usd * 100) // 100
    # but watch for float precision. int() truncates toward zero which
    # is the same as floor for positive numbers.
    cents = int(usd * 100) / 100
    return "$" + f"{cents:.2f}"


def main() -> int:
    src = WEB.read_text()

    # 1. _fmtCost function exists and floors
    m = re.search(r"function _fmtCost\(usd\) \{[\s\S]*?\n        \}", src)
    if not m:
        print("FAIL: _fmtCost function not found")
        return 1
    body = m.group(0)
    if "Math.floor" not in body:
        print("FAIL: _fmtCost doesn't use Math.floor for round-down")
        return 1
    if "toFixed(2)" not in body:
        print("FAIL: _fmtCost doesn't use toFixed(2) for 2-decimal display")
        return 1
    print("OK _fmtCost() uses Math.floor + toFixed(2) for 2-decimal round-down")

    # 2. The 4 cost display locations in renderCostByModel use _fmtCost
    # Find the renderCostByModel function body
    idx = src.find("function renderCostByModel(")
    if idx < 0:
        print("FAIL: couldn't find renderCostByModel")
        return 1
    open_b = src.find("{", idx)
    depth = 1
    i = open_b + 1
    while i < len(src) and depth > 0:
        if src[i] == "{": depth += 1
        elif src[i] == "}": depth -= 1
        i += 1
    cbm_body = src[open_b:open_b + (i - open_b)]

    # 2a. KPI total cost uses _fmtCost
    if "_fmtCost(totalCost)" not in cbm_body:
        print("FAIL: KPI total cost doesn't use _fmtCost(totalCost)")
        return 1
    if "totalCost.toFixed" in cbm_body:
        print("FAIL: KPI total cost still has raw totalCost.toFixed() — should use _fmtCost")
        return 1
    print("OK KPI total cost uses _fmtCost(totalCost)")

    # 2b. Bar chart value label uses _fmtCost
    # Look for the bar chart's value drawing (the "fillText" call)
    bar_label_match = re.search(r"fillText\(.*?barW\s*\+\s*6", cbm_body)
    if not bar_label_match:
        print("FAIL: couldn't find the bar chart value label")
        return 1
    bar_label_line = cbm_body[bar_label_match.start():bar_label_match.start() + 200]
    if "_fmtCost" not in bar_label_line:
        print(f"FAIL: bar chart value label doesn't use _fmtCost")
        print(f"      got: {bar_label_line[:120]}")
        return 1
    if "v.toFixed" in bar_label_line:
        print(f"FAIL: bar chart value label still has raw v.toFixed() — should use _fmtCost")
        return 1
    print("OK bar chart value label uses _fmtCost")

    # 2c. $ cost list uses _fmtCost
    if "_fmtCost(m.hardcoded_cost_usd)" not in cbm_body:
        print("FAIL: $ cost list doesn't use _fmtCost")
        return 1
    if re.search(r"hardcoded_cost_usd.*toFixed\(4\)", cbm_body):
        print("FAIL: $ cost list still has raw hardcoded_cost_usd.toFixed(4)")
        return 1
    print("OK $ cost list uses _fmtCost(m.hardcoded_cost_usd)")

    # 2d. Detail table cost column uses _fmtCost
    if "_fmtCost(m.hardcoded_cost_usd)" not in cbm_body.split('id="cbmTable"')[1] if 'id="cbmTable"' in cbm_body else cbm_body:
        # The detail table is rendered after the cost list. We just
        # need to confirm there's no raw hardcoded_cost_usd.toFixed(4)
        # left in the function body at all.
        pass
    if re.search(r"\(m\.hardcoded_cost_usd\)\.toFixed", cbm_body):
        print("FAIL: detail table cost column still has raw .toFixed(4)")
        return 1
    # We expect _fmtCost(m.hardcoded_cost_usd) to appear at least
    # twice (cost list + detail table)
    n_uses = cbm_body.count("_fmtCost(m.hardcoded_cost_usd)")
    if n_uses < 2:
        print(f"FAIL: expected _fmtCost(m.hardcoded_cost_usd) to appear at least 2x in renderCostByModel, found {n_uses}")
        return 1
    print(f"OK _fmtCost(m.hardcoded_cost_usd) appears {n_uses}x in renderCostByModel (cost list + detail table)")

    # 3. Re-impl the JS in Python and verify the canonical cases
    print()
    print("Re-implementing _fmtCost in Python and testing the canonical cases:")
    cases = [
        (0,        "$0.00"),
        (0.001,    "$0.00"),   # sub-cent
        (0.005,    "$0.00"),   # sub-cent
        (0.01,     "$0.01"),
        (0.1343,   "$0.13"),   # the user's example
        (0.135,    "$0.13"),   # rounds down, NOT 0.14
        (0.1399,   "$0.13"),   # rounds down, NOT 0.14
        (0.99,     "$0.99"),
        (1.0,      "$1.00"),
        (1.5678,   "$1.56"),
        (12.345,   "$12.34"),
        (100,      "$100.00"),
        (1234.5678,"$1234.56"),
    ]
    all_ok = True
    for usd, expected in cases:
        got = _fmt_cost_py(usd)
        ok = (got == expected)
        if not ok:
            all_ok = False
            print(f"  FAIL: _fmtCost({usd}) = {got!r}, expected {expected!r}")
        else:
            print(f"  OK   _fmtCost({usd}) = {got}")
    if not all_ok:
        return 1
    print("OK all 13 canonical _fmtCost cases pass (including the user's 0.1343 -> 0.13)")

    # 4. AGENTS.md relative-path discipline
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
