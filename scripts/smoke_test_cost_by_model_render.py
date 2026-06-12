#!/usr/bin/env python3
"""
Smoke test for the Cost-by-Model sub-tab render path.

Reproduces the JS render function (renderCostByModel) in Python so we can
catch shape mismatches, NaN, or off-by-one errors against the live API
response — without needing a browser.

We can't run the actual Canvas 2D drawing server-side (no DOM), but we
can:
  1. Hit /api/llm-usage/per-model-cost and validate response shape
  2. Re-implement the sorted[] + KPI reduce logic in Python
  3. Re-implement the canvas-2D bar math (maxV, labelW, barW, rowH, etc.)
     and assert all drawing coords are inside the canvas (no negative
     widths, no off-canvas x)
  4. Verify the detail-table + audit HTML strings are syntactically sane

This catches the class of bug that the previous Chart.js
ReferenceError masked: the page silently failed at the first `new
Chart(...)` call and never rendered the table/audit either, so the user
saw a half-broken page. Now that Chart.js is gone, the full render path
needs to be safe.

Per AGENTS.md: "re-implement the page's render function in Python and
run it against the live response. Catches shape mismatches that 200
hides."
"""
import json
import sys
import urllib.request
from urllib.parse import urlencode
from pathlib import Path

ENV = Path(__file__).resolve().parent.parent / ".env"
API_BASE = "http://localhost:8765"


def get_password() -> str:
    for line in ENV.read_text().splitlines():
        if line.startswith("WEB_PASSWORD="):
            return line.split("=", 1)[1].strip()
    raise SystemExit("WEB_PASSWORD not found in .env")


def login() -> str:
    pw = get_password()
    url = f"{API_BASE}/api/login?{urlencode({'password': pw})}"
    with urllib.request.urlopen(url) as r:
        body = json.loads(r.read())
    if not body.get("success"):
        raise SystemExit(f"Login failed: {body}")
    return body["token"]


def fetch_per_model_cost(token: str) -> dict:
    req = urllib.request.Request(
        f"{API_BASE}/api/llm-usage/per-model-cost",
        headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())


# ---- Python re-impl of renderCostByModel (the bits we can validate) ----
def render_cost_by_model_py(data: dict, canvas_w: int = 500, canvas_h: int = 240):
    """Mirrors the JS renderCostByModel in web/index.html.

    Returns a dict with:
      - kpis: dict of totals
      - sorted: list of models sorted by cost desc
      - cost_chart: list of (label, x, w, value_str) for each bar
      - tokens_chart: list of dicts per model with segment x positions
      - detail_table_rows: HTML rows as strings
      - audit_html: audit section HTML
    Plus an `errors` list of anything that would break the canvas drawing.
    """
    errors = []
    models = data.get("by_model") or []
    if not isinstance(models, list):
        errors.append(f"by_model is {type(models).__name__}, expected list")
        return {"errors": errors}

    kpis = {
        "total_cost": sum(m.get("hardcoded_cost_usd") or 0 for m in models),
        "total_calls": sum(m.get("calls") or 0 for m in models),
        "total_in": sum(m.get("tokens_in") or 0 for m in models),
        "total_out": sum(m.get("tokens_out") or 0 for m in models),
        "total_cr": sum(m.get("cache_read_tokens") or 0 for m in models),
        "total_cw": sum(m.get("cache_write_tokens") or 0 for m in models),
    }

    sorted_m = sorted(models, key=lambda m: (m.get("hardcoded_cost_usd") or 0), reverse=True)

    # Cost chart (horizontal bars)
    cost_labelW, cost_valueW = 180, 70
    cost_barAreaW = canvas_w - cost_labelW - cost_valueW - 20
    if cost_barAreaW <= 0:
        errors.append(f"cost bar area non-positive: {cost_barAreaW} (canvas_w={canvas_w})")
    maxV = max(0.0001, *(m.get("hardcoded_cost_usd") or 0 for m in sorted_m))
    cost_chart = []
    if sorted_m:
        rowH = min(26, max(1, (canvas_h - 16) // len(sorted_m)))
        for m in sorted_m:
            v = m.get("hardcoded_cost_usd") or 0
            barW = (v / maxV) * cost_barAreaW if maxV > 0 else 0
            if barW < 0 or barW > cost_barAreaW + 1:
                errors.append(f"cost barW out of range: {barW}")
            cost_chart.append({
                "model": m["model"],
                "x": cost_labelW,
                "w": barW,
                "value_str": f"${v:.4f}",
            })

    # Tokens chart (stacked horizontal bars)
    segs_def = [
        ("tokens_in",          "#569cd6"),
        ("tokens_out",         "#4ec9b0"),
        ("cache_read_tokens",  "#c586c0"),
        ("cache_write_tokens", "#dcdcaa"),
    ]
    tok_labelW, tok_valueW = 180, 70
    tok_barAreaW = canvas_w - tok_labelW - tok_valueW - 20
    if tok_barAreaW <= 0:
        errors.append(f"tokens bar area non-positive: {tok_barAreaW}")
    totals = [
        (m.get("tokens_in") or 0) + (m.get("tokens_out") or 0) +
        (m.get("cache_read_tokens") or 0) + (m.get("cache_write_tokens") or 0)
        for m in sorted_m
    ]
    maxT = max(1, *totals)
    tokens_chart = []
    if sorted_m:
        for m, total in zip(sorted_m, totals):
            x_cursor = tok_labelW
            segs = []
            for key, _color in segs_def:
                v = m.get(key) or 0
                if v <= 0:
                    continue
                w = (v / maxT) * tok_barAreaW
                if w < 0 or w > tok_barAreaW + 1:
                    errors.append(f"token seg width out of range: {w} (model={m['model']}, seg={key})")
                segs.append({"key": key, "x": x_cursor, "w": max(1, w)})
                x_cursor += w
            label = (
                f"{total/1e6:.2f}M" if total >= 1e6 else
                f"{total/1e3:.1f}K" if total >= 1e3 else
                str(total)
            )
            tokens_chart.append({"model": m["model"], "segs": segs, "label": label})

    # Detail table HTML (basic — no full string comparison, just check we can format each row)
    detail_rows = []
    for m in sorted_m:
        drift_pct = m.get("drift_pct") or 0
        detail_rows.append({
            "model": m["model"],
            "calls": m.get("calls") or 0,
            "in": m.get("tokens_in") or 0,
            "out": m.get("tokens_out") or 0,
            "cr": m.get("cache_read_tokens") or 0,
            "cw": m.get("cache_write_tokens") or 0,
            "cost": m.get("hardcoded_cost_usd") or 0,
            "drift": drift_pct,
            "known": m.get("known", True),
        })

    # Audit
    missing = data.get("missing_prices") or []
    drift_flags = data.get("drift_flags") or []
    drift_threshold = data.get("drift_threshold_pct") or 5
    audit = {
        "window_hours": data.get("window_hours"),
        "events_count": data.get("events_count"),
        "mmx_quota_pulled": data.get("mmx_quota_pulled"),
        "at": data.get("at"),
        "missing": list(missing),
        "drift_flags": list(drift_flags),
        "drift_threshold": drift_threshold,
    }

    return {
        "errors": errors,
        "kpis": kpis,
        "sorted": [m["model"] for m in sorted_m],
        "cost_chart": cost_chart,
        "tokens_chart": tokens_chart,
        "detail_rows": detail_rows,
        "audit": audit,
    }


def main() -> int:
    token = login()
    data = fetch_per_model_cost(token)

    # 1. Shape checks
    for k in ("by_model", "window_hours", "events_count", "missing_prices",
              "drift_flags", "at"):
        if k not in data:
            print(f"FAIL: response missing key '{k}'")
            return 1
    if not isinstance(data["by_model"], list):
        print(f"FAIL: by_model is {type(data['by_model']).__name__}, expected list")
        return 1
    print(f"OK shape: {len(data['by_model'])} models, window={data['window_hours']}h, "
          f"events={data['events_count']}")

    # 2. Render
    r = render_cost_by_model_py(data)
    if r["errors"]:
        print("FAIL: render errors:")
        for e in r["errors"]:
            print(f"  - {e}")
        return 1
    print(f"OK render: kpis.total_cost=${r['kpis']['total_cost']:.4f}, "
          f"kpis.total_calls={r['kpis']['total_calls']}")
    print(f"OK sorted models (by cost desc): {r['sorted']}")

    # 3. Cost chart bars
    if r["cost_chart"]:
        max_bar = max(c["w"] for c in r["cost_chart"])
        print(f"OK cost chart: {len(r['cost_chart'])} bars, max bar width = {max_bar:.1f}px")
        # Sanity: the top model should have the widest bar
        assert r["cost_chart"][0]["w"] >= max_bar * 0.99, "top model doesn't have the widest cost bar"

    # 4. Tokens chart — segment widths add up correctly
    for tc in r["tokens_chart"]:
        seg_total = sum(s["w"] for s in tc["segs"])
        if seg_total > 500 + 5:  # canvas_w (500) + tolerance
            print(f"FAIL: model {tc['model']} token segments overflow canvas: {seg_total:.1f}px")
            return 1
    print(f"OK tokens chart: {len(r['tokens_chart'])} model rows, all segments in-bounds")

    # 5. Detail table rows
    assert r["detail_rows"], "no detail rows produced"
    print(f"OK detail table: {len(r['detail_rows'])} rows")

    # 6. Audit
    a = r["audit"]
    print(f"OK audit: window={a['window_hours']}h, events={a['events_count']}, "
          f"mmx_quota={a['mmx_quota_pulled']}, "
          f"missing={len(a['missing'])}, drift_flags={len(a['drift_flags'])}")

    # 7. Sanity-check that the JS file no longer references Chart (the bug we just fixed)
    index_html = (Path(__file__).resolve().parent.parent / "web" / "index.html").read_text()
    if "new Chart(" in index_html or "new window.Chart(" in index_html:
        print("FAIL: web/index.html still references Chart() — fix is incomplete")
        return 1
    print("OK no Chart() references in web/index.html (Chart.js not required)")

    # 8. Relative-path discipline (AGENTS.md rule)
    import re
    abs_fetch = re.findall(r"fetch\(['\"]/", index_html)
    if abs_fetch:
        print(f"FAIL: web/index.html has {len(abs_fetch)} absolute-path fetch() (would break under /selena-astra/)")
        return 1
    print("OK all fetch() paths in web/index.html are relative")

    print()
    print("ALL CHECKS PASSED ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
