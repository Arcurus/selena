#!/usr/bin/env python3
"""
Normalize entity stats for Open World Selena
============================================

Steady-state stats cap.  For every entity, sum all integer properties
(including `power`) and compare against the cap:

    cap = max(1, power * 5) + 100

If an entity is over the cap, the script can PREVIEW what would change
or NORMALIZE all values proportionally so the new sum exactly equals
the cap.  This is the script-half of the rule from Arcurus 2026-06-07
#openworld; the runtime effect path (Rust) only WARNS, it does not
normalize (so a single big effect doesn't silently shrink an entity
mid-action).

Cap formula rationale: see the docstring in
`open-world-selena/src/main.rs` (STATS_CAP_POWER_MULTIPLIER /
STATS_CAP_BASE / STATS_CAP_POWER_FLOOR).  The script mirrors those
constants so the wire format and the Rust code stay in sync.  If you
change one, change the other.

API
---
The script talks to the open-world HTTP API (port 8081, shared-secret
cookie auth).  The open-world server accepts the literal cookie
`openworld_auth=1` (see `verify_auth_cookie` in
`open-world-selena/src/main.rs`); the service is bound to localhost
only, so the shared secret is a localhost-trust boundary, not a
real auth flow.  No /api/login step is needed; we just send the
cookie on every request.  The script is stateless across runs.

CLI
---
    python3 normalize_stats.py status                  # JSON: who's over their cap
    python3 normalize_stats.py status --format text    # Human-readable table
    python3 normalize_stats.py status --type faction   # Filter by entity type
    python3 normalize_stats.py preview [--type T]      # Dry-run: show what would change
    python3 normalize_stats.py normalize [--type T] [--yes]   # Apply, save world after
    python3 normalize_stats.py apply --id ENTITY_ID    # Normalize one entity
    python3 normalize_stats.py constants               # Print the cap constants (sanity check)

Configuration (env vars, all optional):
    OPEN_WORLD_HOST     — base URL (default: http://localhost:8081)
    NORMALIZE_STATS_DRY_RUN — if "1", `normalize` becomes a preview
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
SELENA_ROOT = os.path.abspath(os.path.join(HERE, ".."))

# ---------------------------------------------------------------------------
# Cap constants — MUST mirror the Rust constants in src/main.rs
# ---------------------------------------------------------------------------

STATS_CAP_POWER_MULTIPLIER = 5
STATS_CAP_BASE = 100
STATS_CAP_POWER_FLOOR = 1


def compute_cap(power: int) -> int:
    """Mirror of `compute_stats_cap` in src/main.rs."""
    return max(STATS_CAP_POWER_FLOOR, int(power)) * STATS_CAP_POWER_MULTIPLIER + STATS_CAP_BASE


def stats_sum(props_int: Dict[str, int]) -> int:
    """Signed sum of all integer properties (power counts)."""
    return sum(int(v) for v in props_int.values())


# ---------------------------------------------------------------------------
# API client
# ---------------------------------------------------------------------------

DEFAULT_HOST = "http://localhost:8081"
# The open-world server's verify_auth_cookie checks for this
# literal cookie name=value pair.  No /api/login is involved; the
# service is trusted on localhost.
AUTH_COOKIE = "openworld_auth=1"
AUTH_HEADER = f"Cookie: {AUTH_COOKIE}"


def _api_get(host: str, path: str) -> Dict[str, Any]:
    url = f"{host}{path}"
    req = urllib.request.Request(
        url,
        headers={"Cookie": AUTH_COOKIE},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        print(f"ERROR: GET {url} → HTTP {e.code}: {body}", file=sys.stderr)
        sys.exit(2)


def _api_put_int_property(
    host: str,
    entity_id: str,
    key: str,
    value: int,
) -> Dict[str, Any]:
    """PUT /api/entities/:id/properties/int/:key with {"value": N}."""
    url = f"{host}/api/entities/{entity_id}/properties/int/{urllib.parse.quote(key)}"
    data = json.dumps({"value": int(value)}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Cookie": AUTH_COOKIE, "Content-Type": "application/json"},
        method="PUT",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        print(f"ERROR: PUT {url} (value={value}) → HTTP {e.code}: {body}", file=sys.stderr)
        return {"success": False, "error": f"HTTP {e.code}: {body}"}


def _api_post(host: str, path: str) -> Dict[str, Any]:
    url = f"{host}{path}"
    req = urllib.request.Request(
        url,
        headers={"Cookie": AUTH_COOKIE},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        print(f"ERROR: POST {url} → HTTP {e.code}: {body}", file=sys.stderr)
        return {"success": False, "error": f"HTTP {e.code}: {body}"}


# ---------------------------------------------------------------------------
# Entity loading + analysis
# ---------------------------------------------------------------------------

def load_all_entities(host: str) -> List[Dict[str, Any]]:
    """Load all entities (paginated).  Returns the list of `data` entries."""
    entities: List[Dict[str, Any]] = []
    offset = 0
    page_size = 200
    while True:
        qs = urllib.parse.urlencode({
            "limit": page_size,
            "offset": offset,
            "include_system": "false",
        })
        body = _api_get(host, f"/api/entities?{qs}")
        data = body.get("data") or []
        entities.extend(data)
        total = body.get("count", len(entities))
        offset += len(data)
        if offset >= total or not data:
            break
    return entities


def analyze_entity(entity: Dict[str, Any]) -> Dict[str, Any]:
    """Compute cap, sum, overage, and the post-normalize new values."""
    props_int = entity.get("properties_int") or {}
    # Make sure all values are int (the API might return some as floats if
    # serialized weirdly through serde).
    clean_int: Dict[str, int] = {k: int(v) for k, v in props_int.items()}
    power = int(clean_int.get("power", 0))
    cap = compute_cap(power)
    total = stats_sum(clean_int)
    overage = total - cap
    over_cap = total > cap
    scale = (cap / total) if (over_cap and total != 0) else 1.0
    new_values: Dict[str, int] = {}
    if over_cap and total != 0:
        for k, v in clean_int.items():
            new_values[k] = int(round(v * scale))
    return {
        "id": entity.get("id"),
        "name": entity.get("name"),
        "entity_type": entity.get("entity_type"),
        "power": power,
        "cap": cap,
        "sum": total,
        "overage": overage,
        "over_cap": over_cap,
        "scale": scale,
        "current": clean_int,
        "new": new_values,
    }


def filter_analyzed(
    analyzed: List[Dict[str, Any]],
    only_over: bool,
    entity_type: Optional[str],
) -> List[Dict[str, Any]]:
    out = analyzed
    if entity_type:
        out = [a for a in out if a["entity_type"] == entity_type]
    if only_over:
        out = [a for a in out if a["over_cap"]]
    return out


# ---------------------------------------------------------------------------
# Output rendering
# ---------------------------------------------------------------------------

def render_table(analyzed: List[Dict[str, Any]]) -> str:
    if not analyzed:
        return "  (no entities matched)"
    # Column widths
    name_w = min(40, max(8, min(len(a["name"] or "?") for a in analyzed) if all(a["name"] for a in analyzed) else 8,
                         max(len(a["name"] or "(no name)") for a in analyzed)))
    header = f"  {'NAME'.ljust(name_w)}  {'TYPE'.ljust(12)}  {'POWER':>5}  {'SUM':>6}  {'CAP':>6}  {'OVER':>6}  ACTION"
    lines = [header, "  " + "-" * (len(header) - 2)]
    for a in analyzed:
        name = (a["name"] or "(no name)")[:name_w].ljust(name_w)
        et = (a["entity_type"] or "?").ljust(12)
        power = str(a["power"]).rjust(5)
        sm = str(a["sum"]).rjust(6)
        cap = str(a["cap"]).rjust(6)
        over = (f"+{a['overage']}" if a["overage"] > 0 else "0").rjust(6)
        if a["over_cap"]:
            action = f"normalize (scale={a['scale']:.4f})"
        else:
            action = "ok"
        lines.append(f"  {name}  {et}  {power}  {sm}  {cap}  {over}  {action}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------

def apply_normalization(
    host: str,
    entity_id: str,
    new_values: Dict[str, int],
    save_world: bool = True,
) -> Dict[str, Any]:
    """PUT each new value via the per-property endpoint, then optionally save."""
    results: List[Dict[str, Any]] = []
    for k, v in new_values.items():
        r = _api_put_int_property(host, entity_id, k, v)
        results.append({"key": k, "value": v, "result": r})
    if save_world:
        sv = _api_post(host, "/api/world/save")
    else:
        sv = {"success": True, "skipped": True}
    return {"properties": results, "save": sv}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def cmd_status(args, host: str) -> int:
    entities = load_all_entities(host)
    analyzed = [analyze_entity(e) for e in entities]
    analyzed = filter_analyzed(analyzed, args.only_over, args.type)
    if args.format == "json":
        # Strip the giant `current` map for the JSON output unless verbose
        out = []
        for a in analyzed:
            d = {k: v for k, v in a.items() if k != "current" and k != "new"}
            if args.verbose:
                d["current"] = a["current"]
                d["new"] = a["new"]
            out.append(d)
        print(json.dumps(out, indent=2, default=str))
    else:
        over_count = sum(1 for a in analyzed if a["over_cap"])
        total_count = len(analyzed)
        print(f"  Stats-cap status: {over_count} over cap / {total_count} total")
        print(f"  Cap formula: max(1, power * {STATS_CAP_POWER_MULTIPLIER}) + {STATS_CAP_BASE}")
        print()
        print(render_table(analyzed))
    return 0


def cmd_preview(args, host: str) -> int:
    entities = load_all_entities(host)
    analyzed = [analyze_entity(e) for e in entities]
    analyzed = filter_analyzed(analyzed, only_over=True, entity_type=args.type)
    if not analyzed:
        print("  (no entities over their cap — nothing to normalize)")
        return 0
    print(f"  Would normalize {len(analyzed)} entit{'y' if len(analyzed) == 1 else 'ies'}:")
    print()
    for a in analyzed:
        name = a["name"] or "(no name)"
        print(f"  • {name} ({a['entity_type']}, id={a['id']})")
        print(f"      power={a['power']}, sum={a['sum']}, cap={a['cap']}, "
              f"overage={a['overage']}, scale={a['scale']:.4f}")
        for k, old in sorted(a["current"].items()):
            new = a["new"].get(k, old)
            delta = new - old
            mark = " " if delta == 0 else ("↓" if delta < 0 else "↑")
            print(f"        {mark} {k:>14}: {old:>8} → {new:>8}  (Δ {delta:+d})")
    return 0


def cmd_normalize(args, host: str) -> int:
    if os.environ.get("NORMALIZE_STATS_DRY_RUN") == "1":
        args.yes = False
        print("  (NORMALIZE_STATS_DRY_RUN=1 set, falling back to preview mode)")
        return cmd_preview(args, host)
    entities = load_all_entities(host)
    analyzed = [analyze_entity(e) for e in entities]
    over = filter_analyzed(analyzed, only_over=True, entity_type=args.type)
    if not over:
        print("  (no entities over their cap — nothing to normalize)")
        return 0
    print(f"  Found {len(over)} entit{'y' if len(over) == 1 else 'ies'} over their cap.")
    if not args.yes:
        print()
        print("  Run with --yes to apply, or `normalize_stats.py preview` to see the diff first.")
        print()
        cmd_preview(args, host)
        return 1
    print()
    print("  Applying:")
    n_ok = 0
    n_fail = 0
    for a in over:
        result = apply_normalization(host, a["id"], a["new"], save_world=False)
        all_ok = all(r["result"].get("success") for r in result["properties"])
        if all_ok:
            n_ok += 1
            print(f"    ✓ {a['name']} ({a['id']}) — {len(result['properties'])} props")
        else:
            n_fail += 1
            print(f"    ✗ {a['name']} ({a['id']}) — see errors above")
    # Save once at the end (cheaper than per-entity)
    print()
    print("  Saving world…")
    sv = _api_post(host, "/api/world/save")
    if sv.get("success"):
        print(f"    ✓ saved: {sv.get('path', '?')} ({sv.get('size_mb', '?')})")
    else:
        print(f"    ✗ save failed: {sv.get('error', '?')}")
    print()
    print(f"  Done: {n_ok} normalized, {n_fail} failed")
    return 0 if n_fail == 0 else 2


def cmd_apply(args, host: str) -> int:
    """Normalize a single entity by id."""
    body = _api_get(host, f"/api/entities/{urllib.parse.quote(args.id)}")
    if not body.get("success") or not body.get("data"):
        print(f"  ERROR: entity {args.id} not found", file=sys.stderr)
        return 2
    entity = body["data"]
    a = analyze_entity(entity)
    if not a["over_cap"]:
        print(f"  {a['name']} is within cap (sum={a['sum']}, cap={a['cap']}); nothing to do.")
        return 0
    print(f"  {a['name']} ({a['entity_type']}): sum={a['sum']}, cap={a['cap']}, "
          f"overage={a['overage']}, scale={a['scale']:.4f}")
    for k, old in sorted(a["current"].items()):
        new = a["new"].get(k, old)
        delta = new - old
        mark = " " if delta == 0 else ("↓" if delta < 0 else "↑")
        print(f"    {mark} {k:>14}: {old:>8} → {new:>8}  (Δ {delta:+d})")
    if not args.yes:
        print()
        print("  Run with --yes to apply.")
        return 1
    result = apply_normalization(host, args.id, a["new"], save_world=not args.no_save)
    if not args.no_save:
        sv = result["save"]
        if sv.get("success"):
            print(f"  ✓ saved: {sv.get('path', '?')} ({sv.get('size_mb', '?')})")
        else:
            print(f"  ✗ save failed: {sv.get('error', '?')}")
    else:
        print("  (world not saved; --no_save)")
    return 0


def cmd_constants(args, host: str) -> int:
    out = {
        "STATS_CAP_POWER_MULTIPLIER": STATS_CAP_POWER_MULTIPLIER,
        "STATS_CAP_BASE": STATS_CAP_BASE,
        "STATS_CAP_POWER_FLOOR": STATS_CAP_POWER_FLOOR,
        "formula": f"cap = max({STATS_CAP_POWER_FLOOR}, power * {STATS_CAP_POWER_MULTIPLIER}) + {STATS_CAP_BASE}",
        "examples": {
            "power=0": compute_cap(0),
            "power=1": compute_cap(1),
            "power=10": compute_cap(10),
            "power=100": compute_cap(100),
            "power=197 (Ironforge Clan)": compute_cap(197),
            "power=1000": compute_cap(1000),
        },
    }
    print(json.dumps(out, indent=2))
    return 0


def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="normalize_stats",
        description="Open World Selena — entity stats cap normalizer (Arcurus 2026-06-07 #openworld)",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_status = sub.add_parser("status", help="Show stats-cap status for all entities")
    p_status.add_argument("--format", choices=("text", "json"), default="text")
    p_status.add_argument("--type", help="Filter by entity_type (e.g. faction, character, location)")
    p_status.add_argument("--only-over", action="store_true", help="Show only entities over their cap")
    p_status.add_argument("--verbose", action="store_true", help="Include current/new values in JSON output")
    p_status.set_defaults(func=cmd_status, only_over=False)

    p_preview = sub.add_parser("preview", help="Dry-run: show what normalize would change")
    p_preview.add_argument("--type", help="Filter by entity_type")
    p_preview.set_defaults(func=cmd_preview)

    p_normalize = sub.add_parser("normalize", help="Apply normalization to all entities over cap")
    p_normalize.add_argument("--type", help="Filter by entity_type")
    p_normalize.add_argument("--yes", action="store_true", help="Skip confirmation prompt")
    p_normalize.set_defaults(func=cmd_normalize)

    p_apply = sub.add_parser("apply", help="Normalize a single entity by id")
    p_apply.add_argument("--id", required=True, help="Entity UUID")
    p_apply.add_argument("--yes", action="store_true", help="Skip confirmation prompt")
    p_apply.add_argument("--no-save", action="store_true", help="Don't save the world after applying")
    p_apply.set_defaults(func=cmd_apply)

    p_const = sub.add_parser("constants", help="Print the cap formula constants (sanity check)")
    p_const.set_defaults(func=cmd_constants)

    args = parser.parse_args(argv[1:])

    host = os.environ.get("OPEN_WORLD_HOST", DEFAULT_HOST).rstrip("/")
    # No login step — the open-world server uses a shared-secret cookie
    # (openworld_auth=1) and is bound to localhost.

    return args.func(args, host)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
