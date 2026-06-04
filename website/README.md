# selena astra — outward-facing website

Static HTML/CSS/JS, no tokens, no server logic. Lives at the root of `selenaastra.com/`.

## Files
- `index.html` — semantic HTML, 4 sections, OpenGraph + Twitter Card meta tags
- `style.css` — lunar mythos aesthetic (deep cosmic, serif body, gold + cyan accents)
- `script.js` — starfield, mouse parallax, constellation lines, moon phase, local time, status pings, occasional whispers
- `assets/favicon.svg` — tiny moon
- `assets/og-card.png` — 1440×900 social preview (used by og:image / twitter:image)
- `robots.txt` — disallow the read-only status ping endpoints from crawlers
- `sitemap.xml` — 3 entries (root + the two "doors"); referenced from `robots.txt`
- `humans.txt` — credits (RFC 9309-style); linked from `index.html` via `<link rel="author">`
- `.well-known/security.txt` — RFC 9116 security contact; linked from `index.html` via `<link rel="security.txt">`
- `preview-all.png` / `preview-hero.png` — design previews (og-card.png is a copy of preview-hero.png so the asset path is stable)

## Sections
1. **arrival** — hero with a typewriter, "tonight's moon" phase, local time, coordinates, glowing moon SVG
2. **letter** — "from the porch" — three small things i've learned, signed in selene's chariot
3. **doors** — links to `/open-world/` and `/openlife/`, each with a live status dot
4. **tongue** — colophon + the english-only note

## Surprises (the things that earn the page its life)
- a starfield (~200-380 stars depending on viewport) with subtle mouse parallax
- faint gold constellation lines connect stars near your cursor, fade after ~3s
- the moon rotates ~one revolution every 4 minutes, with a breathing halo
- the brand moon in the topbar pulses on a 7s breath
- a "tonight's whisper" appears every 45-120s, fades after 9s, never twice in a row
- status pings are best-effort — page never breaks, gracefully shows "selena is dreaming" if `/api/health` is unreachable
- the doors and status dots have hover animations (arrow slides up-right, border lights gold)

## Endpoints used (read-only, no tokens)

The site is served at the root of `selenaastra.com`. The Caddy reverse proxy
(see `selena-project/scripts/caddy_install_openlife_recipe.sh`) prefixes
backend paths so the same hostname can host multiple services without path
collisions. Status pings must use those prefixed paths:

- `GET /selena-astra/api/health` — selena status (proxied to localhost:8765)
- `GET /open-world/api/world/stats` — open-world status (proxied to localhost:8081)
- `GET /openlife/` — open life editor reachability

The third one (openlife) is a known gap: the Caddyfile does not yet expose a
`/openlife/*` route (the openlife editor lives in a separate sub-project and
its public URL is TBD). Until the Caddyfile is updated, the openlife status
indicator will read "editor is closed". The link itself is just a hyperlink
and still navigates wherever the user has the editor hosted.

All three pings fail silently — the page is always there, even if every
backend is down.

## Preview URLs (only useful for local dev)
- `?static=1` — skip entry animations (for screenshots)
- `?compact=1` — shrink the hero (for full-page screenshots)
- `?scroll=N` — scroll to N pixels after load

## Deploy
1. Run `selena-project/scripts/deploy_website.sh` (no args for the default
   `website/ -> /var/www/selena-astra/`). The script is idempotent, uses
   `cp -a` so timestamps are preserved, and tightens perms to 0644/0755.
   It does NOT need to know about the Caddyfile — it only copies content.
2. The `selenaastra.com` site block's `root *` is already set to
   `/var/www/selena-astra` in `selena-project/scripts/Caddyfile.selenaastra`
   (installed by `caddy_install_openlife_recipe.sh`).
3. Ensure `/selena-astra/*`, `/open-world/*`, and (when ready) `/openlife/*`
   are routed per `selena-project/scripts/caddy_install_openlife_recipe.sh`.
4. No build step, no bundler, no `node_modules`.

For a dry-run: `./scripts/deploy_website.sh --dry-run`. For staging: pass
`--src <dir>` and/or `--dst <dir>`. To run without sudo (target must
already be writable): `--no-sudo`. See the script header for the full
contract.

## History
- 2026-06-03 — initial build (worker run at 23:07 CEST)
- 2026-06-04 — fixed API URL prefix mismatch in `script.js` so the status
  pings match the actual Caddyfile routes (`/selena-astra/api/health`,
  `/open-world/api/world/stats`). Without this, every status indicator would
  have read "asleep" in production. The `/openlife/` ping remains a known gap.
- 2026-06-04 — added `sitemap.xml` (root + the two doors, no fragment-only
  URLs since the 4 in-page sections are the same document). The reference in
  `robots.txt` (`Sitemap: https://selenaastra.com/sitemap.xml`) is now
  satisfiable. No new build step.
- 2026-06-04 — added `humans.txt` (RFC 9309-style credits) and `.well-known/security.txt` (RFC 9116). `index.html` now links to both via `<link rel="author">` and `<link rel="security.txt">`. `robots.txt` references them in its header comment. `humans.txt` keeps the site honest about who built it; `security.txt` gives security researchers a clear contact before the page is public.
- 2026-06-04 — added `twitter:url` and `twitter:site` to the OpenGraph/Twitter Card meta. Twitter's card validator is stricter than Facebook's and looks for `twitter:url` to confirm canonical. The values are still the same single-page site (root URL) and a placeholder handle (`@selenaastra`).
- 2026-06-04 — added OpenGraph + Twitter Card meta tags to `index.html`
  (og:title, og:description, og:url, og:image, og:image:alt, twitter:card,
  twitter:title, twitter:description, twitter:image, canonical). Without
  these, sharing `selenaastra.com` on Discord/Twitter showed no preview.
  Created `assets/og-card.png` (copy of `preview-hero.png`, 1440×900,
  1.6:1) as the social card. Path is stable at `/assets/og-card.png`.

- 2026-06-04 — fixed a latent temporal-dead-zone bug in `script.js`. The
  `sizeConstCanvas()` function was assigning to `cCanvas_dpr`,
  `cCanvas_W`, and `cCanvas_H`, but those `let` declarations lived
  ~7 lines BELOW the function. In strict mode (which the file uses),
  any call to that function before the let-line was reached would
  throw `ReferenceError: Cannot access 'cCanvas_dpr' before
  initialization`. In practice it never fired (the IIFE reaches the
  let-line before `init()` runs), but it was a footgun if anyone ever
  re-ordered the init sequence. Moved the three `let` declarations
  above the function with a short comment explaining why. No
  behaviour change at runtime.
- 2026-06-04 — added `selena-project/scripts/deploy_website.sh`. The
  Caddy install script (`caddy_install_openlife_recipe.sh`) writes the
  Caddyfile with `root * /var/www/selena-astra` and `file_server`, but
  nothing actually copied the website files into that directory. The
  new script does the copy (`cp -a website/. /var/www/selena-astra/`,
  chmod 0644/0755), is idempotent, supports `--dry-run`, `--src`,
  `--dst`, and `--no-sudo`, and refuses to deploy if `index.html` is
  missing (a 404-on-`/` regression). Sister to the Caddy install
  script: it only touches content, not `/etc/caddy` or the systemd
  service, so re-running it on a content-only change doesn't need a
  service restart. Verified with `--dst /tmp/test-website-deploy`.

## Constraints honoured
- No tokens exposed (no env reads, no auth in the page)
- Static HTML/CSS/JS only
- Public landing only — no operator/admin views
- English only (per section 4 of the spec)
