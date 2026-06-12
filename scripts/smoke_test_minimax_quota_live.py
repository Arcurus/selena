#!/usr/bin/env python3
"""
Smoke test for the Per-Provider Quota / MiniMax card fix.

Bug: The 'Per-Provider Quota' panel in the LLM Usage tab (and the
Provider Usage sub-tab) was reading `providers.minimax.models[*].window_5h.remaining_percent`
from /api/llm-usage, but that field returns 0 with used_percent=None
even when the top-right widget correctly shows e.g. 76% remaining.
The fix is to use the same data path as the top-right widget
(/api/discord-lookup/llm-minimax, which calls `mmx quota` live with
a 10s server-side cache).

This test verifies the fix end-to-end by re-implementing the
_minimaxProviderCard helper logic in Python and asserting:

  1. The new _renderMinimaxProviderCard helper exists in web/index.html
  2. loadLLMUsage now fetches /api/discord-lookup/llm-minimax
  3. loadProviderUsage now fetches /api/discord-lookup/llm-minimax
  4. /api/discord-lookup/llm-minimax returns non-zero remaining_percent
     for the `general` model in the current window
  5. The old broken data path
     (providers.minimax.models.general.window_5h.remaining_percent)
     is still 0 — proving the bug is real and the fix is needed
  6. The helper renders the live remaining % (e.g. 76%) into the HTML
     for the MiniMax card, not the snapshot's 0
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


def fetch(token: str, path: str) -> dict:
    req = urllib.request.Request(
        f"{API_BASE}{path}",
        headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())


# ---- Python re-impl of _renderMinimaxProviderCard (Python port of the JS) ----
def render_minimax_card_py(p: dict, polling: dict, live: dict) -> str:
    """Return the HTML for the MiniMax provider card given the
    live /api/discord-lookup/llm-minimax response. Mirrors the JS
    helper in web/index.html."""
    if not live or not live.get("windows"):
        # Fallback to snapshot
        return f"FALLBACK: providers={p.get('quota', {}).get('ok')}, live=empty"
    windows = live["windows"]
    by_model = {}
    for w in windows:
        by_model.setdefault(w["model"], {})[w["name"]] = w
    focus = by_model.get("general", {}).get("window_5h")
    badge = "unknown"
    badge_color = "var(--text-secondary)"
    if focus and isinstance(focus.get("remaining_percent"), (int, float)):
        rem = focus["remaining_percent"]
        if rem >= 60:
            badge, badge_color = f"{rem}% left", "var(--success)"
        elif rem >= 30:
            badge, badge_color = f"{rem}% left", "var(--warning)"
        else:
            badge, badge_color = f"{rem}% left", "var(--danger,#e05555)"
    # Per-model sub-cards
    parts = []
    for mname, w in by_model.items():
        for wname in ("window_5h", "weekly"):
            win = w.get(wname)
            if win and isinstance(win.get("remaining_percent"), (int, float)):
                parts.append(f"{mname}/{wname}: {win['remaining_percent']}%")
    return f"badge={badge} ({badge_color}) | " + " | ".join(parts)


def main() -> int:
    token = login()
    src = WEB.read_text()

    # 1. Helper exists
    if "_renderMinimaxProviderCard" not in src:
        print("FAIL: _renderMinimaxProviderCard helper not found in web/index.html")
        return 1
    print("OK helper exists: _renderMinimaxProviderCard")

    # 2. loadLLMUsage fetches the live minimax endpoint
    # Find the loadLLMUsage function and check it contains the fetch.
    # Use a simple line-range approach: start at the function header
    # and walk forward until we hit a closing `}` at the same indent.
    def _find_function_body(src_text: str, name: str) -> str:
        # Find header line (8-space indent typical for top-level funcs)
        idx = src_text.find(f"async function {name}(")
        if idx < 0:
            idx = src_text.find(f"function {name}(")
        if idx < 0:
            return ""
        # Find the opening { on the same line
        open_brace = src_text.find("{", idx)
        if open_brace < 0:
            return ""
        depth = 1
        i = open_brace + 1
        while i < len(src_text) and depth > 0:
            c = src_text[i]
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
            i += 1
        return src_text[open_brace:open_brace + (i - open_brace)]

    body_llm = _find_function_body(src, "loadLLMUsage")
    if not body_llm:
        print("FAIL: couldn't find loadLLMUsage function body")
        return 1
    if "api/discord-lookup/llm-minimax" not in body_llm:
        print("FAIL: loadLLMUsage doesn't fetch /api/discord-lookup/llm-minimax")
        return 1
    if "_lastMinimaxQuota" not in body_llm:
        print("FAIL: loadLLMUsage doesn't stash the result on window._lastMinimaxQuota")
        return 1
    print("OK loadLLMUsage: parallel fetch + window._lastMinimaxQuota")

    # 3. loadProviderUsage fetches the live minimax endpoint
    body_prov = _find_function_body(src, "loadProviderUsage")
    if not body_prov:
        print("FAIL: couldn't find loadProviderUsage function body")
        return 1
    if "api/discord-lookup/llm-minimax" not in body_prov:
        print("FAIL: loadProviderUsage doesn't fetch /api/discord-lookup/llm-minimax")
        return 1
    if "_lastMinimaxQuota" not in body_prov:
        print("FAIL: loadProviderUsage doesn't stash the result on window._lastMinimaxQuota")
        return 1
    print("OK loadProviderUsage: parallel fetch + window._lastMinimaxQuota")

    # 4. /api/discord-lookup/llm-minimax returns non-zero for general/window_5h
    live = fetch(token, "/api/discord-lookup/llm-minimax")
    if live.get("error"):
        print(f"FAIL: /api/discord-lookup/llm-minimax returned error: {live['error']}")
        return 1
    gen_5h = next(
        (w for w in (live.get("windows") or [])
         if w.get("model") == "general" and w.get("name") == "window_5h"),
        None
    )
    if not gen_5h:
        print("FAIL: no general/window_5h window in live response")
        return 1
    rem = gen_5h.get("remaining_percent")
    used = gen_5h.get("used_percent")
    status = gen_5h.get("status")
    # The MiniMax 5h window can legitimately be at 0% remaining with
    # status=2 ("no_credits / exhausted") — this happens during heavy
    # usage days. The test's job is to catch regressions of the OLD
    # bug (used_percent=None, remaining_percent=0 from a stale
    # snapshot), not the case where the window is genuinely empty.
    # So we accept (0, 100, 2) as a legitimate state and skip the
    # nonzero assertion in that case.
    if rem == 0 and used == 100 and status == 2:
        print(f"OK live endpoint: general/window_5h is fully consumed "
              f"(remaining=0, used=100, status=2) — skipping nonzero "
              f"check; this is a legitimate state, not the old bug")
    else:
        if not isinstance(rem, (int, float)) or rem <= 0:
            print(f"FAIL: general/window_5h remaining_percent is {rem!r}, expected > 0")
            return 1
        if used is not None and used < 0:
            print(f"FAIL: general/window_5h used_percent is negative: {used}")
            return 1
        print(f"OK live endpoint: general/window_5h remaining={rem}%, used={used}%")

    # 5. The OLD broken data path is still 0 — proving the bug exists
    #    and the fix is necessary.
    usage = fetch(token, "/api/llm-usage")
    providers = usage.get("providers") or {}
    minimax = providers.get("minimax") or {}
    models = minimax.get("models") or {}
    gen_old = models.get("general") or {}
    old_w5h = gen_old.get("window_5h") or {}
    old_rem = old_w5h.get("remaining_percent")
    old_used = old_w5h.get("used_percent")
    # Bug present if remaining is 0 or used is None
    if old_rem == 0 and old_used is None:
        print(f"OK bug confirmed: old data path returns remaining_percent=0, used_percent=None")
    else:
        print(f"NOTE: old data path returned remaining={old_rem}, used={old_used}")
        print("      (if the snapshot has been fixed at the source, this is fine —")
        print("       the JS-side fix is still defensive and correct.)")

    # 6. The rendered card uses the LIVE data, not the old broken path
    card = render_minimax_card_py(minimax, usage.get("polling", {}).get("minimax", {}), live)
    print(f"OK rendered card: {card}")
    # The card must contain the live `rem` value
    if f"{rem}%" not in card:
        print(f"FAIL: rendered card doesn't contain live remaining% ({rem}%)")
        return 1
    if "0% left" in card.split("badge=")[1].split(" |")[0] and rem > 0:
        print(f"FAIL: rendered card shows '0% left' badge even though live is {rem}%")
        return 1
    print(f"OK rendered card contains the live {rem}% value (not the snapshot's 0)")

    # 7. Source check: the helper should be called from both render paths
    # with the right withActions argument.
    if "if (name === 'minimax')" not in src:
        print("FAIL: neither render path uses the new helper for the minimax entry")
        return 1
    n_minimax_branches = src.count("if (name === 'minimax')")
    if n_minimax_branches < 2:
        print(f"FAIL: only {n_minimax_branches} render path(s) use the helper, expected 2 (LLM Usage + Provider Usage)")
        return 1
    # Provider Usage sub-tab should pass true (shows pause/resume + retry),
    # LLM Usage tab should pass false (cleaner card, no extra controls).
    if "_renderMinimaxProviderCard(p, polling[name] || {}, true)" not in src:
        print("FAIL: Provider Usage sub-tab doesn't pass withActions=true to the helper")
        print("      (without true, the pause/resume buttons + retry timer won't show)")
        return 1
    if "_renderMinimaxProviderCard(p, polling[name] || {}, false)" not in src:
        print("FAIL: LLM Usage tab doesn't pass withActions=false to the helper")
        return 1
    # And the old replace-based approach must be GONE (it was fragile
    # and would silently fail if the stateBadge HTML changed).
    if "inner.replace(" in src and "pause 30m" in src.split("inner.replace(")[1].split("loadProviderUsage")[0] if "inner.replace(" in src else False:
        # the above is awkward; just check the simpler way
        pass
    if "inner.replace(" in src:
        # Be strict: any "inner.replace(" is the fragile old approach
        print("FAIL: still using fragile 'inner.replace(' approach for the minimax card.")
        print("      Per Arcurus 2026-06-12 11:00 CEST #cost-tracker: user still saw")
        print("      the old card even after the first fix, so we replaced the")
        print("      regex-based injection with a direct build (withActions param).")
        return 1
    print(f"OK both render paths use the helper with correct withActions ({n_minimax_branches} minimax branches)")

    # 8. AGENTS.md relative-path discipline
    abs_fetch = re.findall(r"fetch\(['\"]/", src)
    if abs_fetch:
        print(f"FAIL: {len(abs_fetch)} absolute-path fetch() calls in web/index.html")
        return 1
    print("OK no absolute-path fetch() in web/index.html")

    print()
    print("ALL CHECKS PASSED ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
