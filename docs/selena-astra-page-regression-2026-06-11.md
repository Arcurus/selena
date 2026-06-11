# Selena-Astra dashboard regression — 2026-06-11

## TL;DR
- **Symptom (reported by Arcurus, 11:34 CET):** the operator dashboard at
  `https://selenaastra.com/selena-astra/` "does not load".
- **Root cause (suspected):** the relative-paths fix from
  commit `b052dfd` (June 4) regressed silently — likely a `web/index.html`
  edit reintroduced a leading `/` in a `fetch()` URL, an `href`, or a CSS
  `url()`. Under the `/selena-astra/` Caddy prefix, an absolute URL like
  `/api/health` resolves against the domain root, not the prefix, and
  Caddy returns 404 for everything except the page itself.
- **Fix this doc introduces:** `scripts/smoke_test_selena_astra.sh` —
  5 cheap HTTP checks that fail loud if the page, its static assets, or
  two representative API endpoints stop responding.

## Why the page breaks — short explanation
The dashboard is served under the Caddy prefix `/selena-astra/`. Caddy
matches that prefix in its `handle` block, strips it with
`uri strip_prefix /selena-astra`, and proxies the rest to the local
`api_server` on port 8765. Caddy has a default `file_server` at `/`, but
that serves `/var/www/selena-astra/` (the static landing-page repo),
not the dashboard. So the page HTML loads from the API server, but a
`fetch('/api/health')` in the page's JS resolves to the domain root,
hits Caddy's default `file_server`, and 404s. Caddy does NOT route
`/api/...` (without the prefix) anywhere — it's the dead zone.

The fix on June 4 (commit `b052dfd`) was to switch all URLs to
**relative** form (`fetch('api/health')` instead of `fetch('/api/health')`).
A browser resolves a relative URL against the current page's directory,
so the same code works under `/selena-astra/`, at `/`, or at any other
Caddy mount.

**The recurring problem:** every later edit to `web/index.html` risks
re-introducing a leading slash. There was no automated check to catch
it. Arcurus's "dont you check if it still runs if you make changes?" is
the right question — we didn't.

## What the smoke test does
A 5-step `curl` walk:

| # | URL | What it catches |
|---|-----|-----------------|
| 1 | `/selena-astra/`                  | page loads (200) and body contains "Selena Astra" |
| 2 | `/selena-astra/static/style.css`  | CSS asset loads (200) |
| 3 | `/selena-astra/static/star-script.js` | starfield JS loads (200) |
| 4 | `/selena-astra/api/health`        | API is alive (200 + `"ok": true`) |
| 5 | `/selena-astra/api/login?password=...` | login round-trip works (200 + `"success": true`) |

It's intentionally cheap — 5 HTTP calls, no JS execution, sub-second
wall time. The login check uses the password from `.env` and throws
the token away. Exit codes: 0 = all pass, 1 = at least one failed,
2 = misuse. The script supports `--json` for machine consumption and
`--host <url>` / positional URL for staging/preview.

## How callers should use it
- **slow-heartbeat / watchdog:** wire into the periodic health check so
  a page regression is reported within minutes, not at "Arcurus opens
  the tab and finds a blank page". Suggested cadence: every 5–15 min.
- **post-deploy / pre-commit:** after any change to `web/index.html`,
  `web/style.css`, the Caddyfile, or `api_server.py`, run
  `./scripts/smoke_test_selena_astra.sh` and treat a non-zero exit as
  a hard failure.
- **on-call debugging:** when a user says "the page is broken", run it
  manually with `--json` and grep for `status":"fail"` to see which
  check broke.

## Known follow-ups (not done in this pass)
- The smoke test does NOT exercise the dashboard's JS — a JS error
  (e.g. an unhandled promise rejection) won't fail it. A headless
  browser check (Playwright / Selenium) would, but that's a separate
  change and out of scope for a one-hour worker run.
- The smoke test does NOT catch a 401-on-everything regression (e.g.
  if auth gate gets accidentally tightened to require login for
  `/api/health`). Worth a one-line addition: a check that
  `/api/health` returns 200 *without* an `Authorization` header. Easy
  follow-up.
- A `pre-commit` hook that runs the smoke test on `web/` changes is
  the cleanest answer to Arcurus's concern, but a git pre-commit hook
  also needs owner-only installation, which crosses into the
  "shared infra" category. Marking as a follow-up for Arcurus's call.

## Related
- todo `3fed6d83` — the original "page does not load" todo.
- todo `5e09d23d` — the "workers should not post no-tokens messages"
  follow-up, separately in flight.
- commit `b052dfd` — the original relative-paths fix.
