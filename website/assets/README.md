# website/assets

Static assets served from `/assets/` on `selenaastra.com/`.

## Files
- `favicon.svg` — the tiny moon in the browser tab. Inline SVG, ~614 B.
- `og-card.png` — 1440×900 OpenGraph / Twitter Card preview image. Shown
  when someone shares `selenaastra.com/` on Discord, Twitter, etc.
  Currently a copy of `../preview-hero.png` (the static hero render). When
  a designed social card is created, replace this file in place — the
  `og:image` and `twitter:image` URLs in `../index.html` already point
  here, so no HTML edit is needed.

## Why the social card lives in `assets/`
- Stable URL: `/assets/og-card.png` is part of the site root, not derived
  from the versioned git history.
- The `assets/` directory is the only static-asset root the website exposes
  (the rest is the four HTML/CSS/JS files at `/`).

## Caddy exposure
The site's Caddy block (see `selena-project/scripts/caddy_install_openlife_recipe.sh`)
serves `root * /var/www/selena-astra`, so anything dropped in this `assets/`
dir on the server is reachable at `https://selenaastra.com/assets/<filename>`.
No additional Caddy route is required.

## Re-rendering the social card
When the website design changes:
1. Re-screenshot the hero (e.g. `?static=1&compact=1` in dev to skip animations)
2. Save the 1440×900 PNG to `preview-hero.png` (root)
3. Re-copy to `assets/og-card.png` to refresh the social preview
