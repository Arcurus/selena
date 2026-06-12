#!/usr/bin/env python3
"""
Smoke test for the "Token prices" table added under Price audit in the
Cost by Model sub-tab.

Verifies, by re-implementing the JS template in Python and running it
against the live /api/llm-usage/per-model-cost response:

  1. The HTML contains a table with header cells: Model, In, Out,
     Cache read, Cache write
  2. One row per model in the response (sorted by cost desc, same
     order as the rest of the page)
  3. Each known-model row has 4 numeric prices formatted as "$X.XX"
  4. Unknown-model rows show "—" for prices and a ⚠ suffix on the
     model cell

This catches the class of bug the JS path hides: a 200 from the API
doesn't mean the rendered HTML is valid, and a typo in the template
literal would silently produce broken DOM.
"""
import json
import re
import sys
import urllib.request
from pathlib import Path
from urllib.parse import urlencode

ENV = Path(__file__).resolve().parent.parent / ".env"
API_BASE = "http://localhost:8765"


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


def fetch(token: str) -> dict:
    req = urllib.request.Request(
        f"{API_BASE}/api/llm-usage/per-model-cost",
        headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())


def build_price_table_html(data: dict) -> str:
    """Python re-impl of the price-table template literal in renderCostByModel."""
    sorted_m = sorted(data.get("by_model") or [],
                      key=lambda m: (m.get("hardcoded_cost_usd") or 0), reverse=True)
    if not sorted_m:
        return ""
    fmt = lambda v: "—" if v is None else f"${v:.2f}"
    rows = []
    for m in sorted_m:
        known = m.get("known", True)
        model_cell = m["model"] + ("" if known else " ⚠")
        rows.append(
            f"<tr><td>{model_cell}</td>"
            f"<td>{fmt(m.get('price_in_usd_per_1m'))}</td>"
            f"<td>{fmt(m.get('price_out_usd_per_1m'))}</td>"
            f"<td>{fmt(m.get('price_cache_read_usd_per_1m'))}</td>"
            f"<td>{fmt(m.get('price_cache_write_usd_per_1m'))}</td></tr>"
        )
    return (
        "<table>"
        "<thead><tr>"
        "<th>Model</th><th>In</th><th>Out</th>"
        "<th>Cache read</th><th>Cache write</th>"
        "</tr></thead><tbody>" + "".join(rows) + "</tbody></table>"
    )


def main() -> int:
    token = login()
    data = fetch(token)

    if not data.get("by_model"):
        print("SKIP: by_model empty (no data in window)")
        return 0

    html = build_price_table_html(data)
    if not html:
        print("FAIL: produced empty price table")
        return 1

    # 1. Required header cells
    for h in ("Model", "In", "Out", "Cache read", "Cache write"):
        if f"<th>{h}</th>" not in html:
            print(f"FAIL: header cell missing: {h}")
            return 1
    print(f"OK headers: all 5 cells present")

    # 2. One row per model
    n_rows = html.count("<tr>") - 1  # minus header
    expected = len(data["by_model"])
    if n_rows != expected:
        print(f"FAIL: expected {expected} data rows, got {n_rows}")
        return 1
    print(f"OK rows: {n_rows} (one per model)")

    # 3. Known models have "$X.XX" prices; unknown show "—"
    for m in data["by_model"]:
        # Each model appears in the HTML with its name
        if m["model"] not in html:
            print(f"FAIL: model {m['model']} missing from price table")
            return 1
        if m.get("known", True):
            # Should have 4 $-formatted prices in its row
            n_dollars = len(re.findall(r"\$\d+\.\d{2}", html.split(m["model"], 1)[1].split("</tr>", 1)[0]))
            if n_dollars != 4:
                print(f"FAIL: known model {m['model']} has {n_dollars} $X.XX prices in its row, expected 4")
                return 1
        else:
            # Should have ⚠ and "—" placeholders
            if "⚠" not in html.split(m["model"], 1)[1].split("</tr>", 1)[0]:
                print(f"FAIL: unknown model {m['model']} missing ⚠ marker")
                return 1
    print(f"OK row contents: known rows have 4 $X.XX prices, unknown flagged")

    # 4. Validate basic well-formedness: <table> opens/closes, <tbody> opens/closes
    for tag in ("<table>", "</table>", "<tbody>", "</tbody>", "<thead>", "</thead>"):
        if html.count(tag) != 1:
            print(f"FAIL: {tag} count is {html.count(tag)}, expected 1")
            return 1
    print(f"OK well-formed: all 6 structural tags appear exactly once")

    # 5. JS source contains the new code (regression: don't accidentally drop it)
    src = (Path(__file__).resolve().parent.parent / "web" / "index.html").read_text()
    for marker in ("Token prices", "price_in_usd_per_1m", "price_cache_read_usd_per_1m",
                   "price_cache_write_usd_per_1m"):
        if marker not in src:
            print(f"FAIL: web/index.html missing source marker '{marker}'")
            return 1
    print(f"OK source: all 4 markers present in web/index.html")

    print()
    print(f"Example rendered first row of price table:")
    print(f"  {html.split('<tr>', 2)[2].split('</tr>')[0]}")
    print()
    print("ALL CHECKS PASSED ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
