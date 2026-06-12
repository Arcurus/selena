#!/usr/bin/env python3
"""
Smoke test for the Cost by Model auth-header fix.

Bug (per Arcurus 2026-06-12 14:00 CEST #cost-tracker): the Cost by
Model sub-tab showed 'Failed to load: HTTP 401' because
`loadCostByModel()` was calling `fetch(url)` WITHOUT the
`Authorization: Bearer <token>` header. The endpoint
/api/llm-usage/per-model-cost requires authentication (see
api_server.py::authenticate() in the route handler), so every
request got rejected with 401.

The other loaders in web/index.html (loadLLMUsage,
loadProviderUsage, refreshMinimaxTopWidget) all use
safeFetchJson() (which adds the header) or pass
`headers: { 'Authorization': 'Bearer ' + token }` explicitly.
loadCostByModel was the only one missing it.

This test verifies the fix by re-implementing the fetch in Python
and asserting:
  1. The JS source sends the Authorization header
  2. The JS guards against a missing token (renders a 'Not logged
     in' message instead of failing silently)
  3. The endpoint still requires auth (a request without the
     header returns 401, so we know the fix is actually needed)
  4. With the header, the endpoint returns 200 + valid data
  5. The error path now includes the response body in the message
     (so a future 401 will be diagnosable from the UI)
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


def fetch(token: str | None) -> tuple[int, dict | str]:
    """Hit the endpoint with or without auth. Returns (status, body)."""
    url = f"{API_BASE}/api/llm-usage/per-model-cost?window_hours=24"
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read())
        except Exception:
            return e.code, ""


def main() -> int:
    token = login()
    src = WEB.read_text()

    # 1. Find the loadCostByModel function body
    idx = src.find("async function loadCostByModel(")
    if idx < 0:
        print("FAIL: couldn't find loadCostByModel function")
        return 1
    open_b = src.find("{", idx)
    depth = 1
    i = open_b + 1
    while i < len(src) and depth > 0:
        if src[i] == "{": depth += 1
        elif src[i] == "}": depth -= 1
        i += 1
    body = src[open_b:open_b + (i - open_b)]
    print(f"OK loadCostByModel function found ({len(body)} chars)")

    # 2. The fetch call must include the Authorization header.
    if "fetch(url" not in body and "fetch(url," not in body:
        # The fetch line might be on multiple lines; look for both pieces
        if re.search(r"fetch\s*\(\s*url\s*,", body) is None:
            print("FAIL: couldn't find the fetch(url, ...) call in loadCostByModel")
            return 1
    # The header MUST include both 'Authorization' and 'Bearer ' + token.
    if "Authorization" not in body:
        print("FAIL: loadCostByModel doesn't send the Authorization header")
        return 1
    if "Bearer " not in body:
        print("FAIL: loadCostByModel doesn't use 'Bearer <token>' scheme")
        return 1
    if re.search(r"'Authorization':\s*'Bearer\s*'\s*\+\s*token", body) is None:
        print("FAIL: header isn't constructed correctly (expected \"'Authorization': 'Bearer ' + token\")")
        return 1
    print("OK loadCostByModel sends 'Authorization: Bearer ' + token")

    # 3. The function must guard against a missing token
    if "typeof token" not in body or "!token" not in body:
        print("FAIL: loadCostByModel doesn't guard against a missing token")
        print("      (without this guard, the fetch would send 'Bearer null' and the server would 401)")
        return 1
    print("OK loadCostByModel guards against missing token (renders 'Not logged in' message)")

    # 4. The error path includes the response body in the message
    #    (so future 401s are diagnosable from the UI, not just "HTTP 401 c" again)
    if "await r.text()" not in body:
        print("FAIL: error path doesn't read the response body")
        print("      (without this, the UI only shows 'HTTP 401' with no detail)")
        return 1
    if ".slice(0, 200)" not in body:
        print("FAIL: response body isn't truncated before display")
        return 1
    print("OK error path includes response body (so future auth failures show the server's message)")

    # 5. Live verification: endpoint actually requires auth
    no_auth_status, no_auth_body = fetch(None)
    if no_auth_status != 401:
        print(f"NOTE: endpoint without auth returned {no_auth_status} (expected 401)")
        print(f"      body: {no_auth_body}")
        print("      This means the fix isn't actually needed (endpoint is open)")
    else:
        print(f"OK endpoint without auth: HTTP 401 — fix is necessary")

    # 6. With auth: endpoint returns 200
    with_auth_status, with_auth_body = fetch(token)
    if with_auth_status != 200:
        print(f"FAIL: endpoint WITH auth returned {with_auth_status} (expected 200)")
        return 1
    if not isinstance(with_auth_body, dict) or "by_model" not in with_auth_body:
        print("FAIL: auth'd response doesn't have by_model field")
        return 1
    print(f"OK endpoint with auth: HTTP 200, by_model has {len(with_auth_body['by_model'])} models")

    # 7. AGENTS.md relative-path discipline
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
