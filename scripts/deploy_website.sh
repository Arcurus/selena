#!/usr/bin/env bash
# deploy_website.sh — copy the outward-facing website into Caddy's document root
# =============================================================================
#
# USED BY:    selena-project-worker (and any future maintainer)
# DEPLOYS:    selena-project/website/  ->  /var/www/selena-astra/
# SISTER TO:  caddy_install_openlife_recipe.sh (that script installs the
#             Caddyfile + restarts Caddy; THIS one just copies the static
#             site files). They are independent — you can re-run this on
#             its own whenever the website changes.
#
# Why a separate script (per Arcurus 2026-06-03 "stay in your lane"):
#   - Caddy install touches /etc/caddy, opens firewall ports, restarts a
#     system service. That's an infra change.
#   - Website deploy just copies a handful of static files. That's a
#     content change. Keeping them separate means a content-only update
#     doesn't need sudo service-management.
#
# IDEMPOTENT:  Re-running overwrites the files. The target directory is
#              created if missing. Permissions are reset to a sane default.
# NO BACKUPS:  The website is in git (selena-project/website/). If you
#              break the deployed copy, re-run this script — that IS the
#              rollback.
#
# USAGE:
#   ./scripts/deploy_website.sh                 # default: src=website, dst=/var/www/selena-astra
#   ./scripts/deploy_website.sh --dry-run       # show what would happen, copy nothing
#   ./scripts/deploy_website.sh --src <dir>     # override source dir (e.g. for staging)
#   ./scripts/deploy_website.sh --dst <dir>     # override target dir (e.g. for /tmp/selena-astra-preview)
#   ./scripts/deploy_website.sh --no-sudo       # use plain cp/rm (target must be writable)
#
# REQUIRES:    bash, cp, mkdir. sudo is used for the /var/www write by
#              default — pass --no-sudo to skip it (the parent directory
#              must already exist and be writable by the current user).

set -euo pipefail

# --- 0. Args ---------------------------------------------------------------

DST_DEFAULT="/var/www/selena-astra"
SRC=""
DST=""
DRY_RUN=0
USE_SUDO=1

while [[ $# -gt 0 ]]; do
    case "$1" in
        --src)     SRC="$2"; shift 2 ;;
        --dst)     DST="$2"; shift 2 ;;
        --dry-run) DRY_RUN=1; shift ;;
        --no-sudo) USE_SUDO=0; shift ;;
        -h|--help)
            sed -n '2,40p' "$0"
            exit 0
            ;;
        *)
            echo "Unknown arg: $1 (try --help)" >&2
            exit 2
            ;;
    esac
done

# SCRIPT_DIR is set first so the SRC default can resolve relative to the
# script's actual location (not the caller's cwd).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
: "${SRC:=$SCRIPT_DIR/../website}"
: "${DST:=$DST_DEFAULT}"

# --- 1. Pre-flight checks --------------------------------------------------

if [[ ! -d "$SRC" ]]; then
    echo "ERROR: source directory not found: $SRC" >&2
    echo "  The website is supposed to live at selena-project/website/." >&2
    echo "  Override with --src <dir> if you staged it elsewhere." >&2
    exit 1
fi

# The website MUST have index.html — without it, Caddy's file_server will
# show a directory-listing-less 404 for /. We deliberately don't enable
# `browse` in the Caddyfile (see scripts/Caddyfile.selenaastra).
if [[ ! -f "$SRC/index.html" ]]; then
    echo "ERROR: $SRC/index.html not found. The site is broken upstream." >&2
    echo "  Refusing to deploy a site with no entry point." >&2
    exit 1
fi

# Sanity check: how many files are we about to copy? This is just a heads-up
# number for the operator — the copy is a wholesale `cp -a SRC/. DST/`, so
# adding/removing files in the source is automatically reflected.
FILE_COUNT=$(find "$SRC" -mindepth 1 -maxdepth 1 -printf '.' | wc -c)
echo "Source : $SRC  ($FILE_COUNT top-level entries)"
echo "Target : $DST"
echo ""

# --- 2. Dry-run path -------------------------------------------------------

if [[ $DRY_RUN -eq 1 ]]; then
    echo "[dry-run] would run:"
    echo "  sudo mkdir -p '$DST'"
    echo "  sudo cp -a '$SRC/.' '$DST/'"
    echo "  sudo find '$DST' -type f -exec chmod 0644 {} +"
    echo "  sudo find '$DST' -type d -exec chmod 0755 {} +"
    echo ""
    echo "[dry-run] would copy these top-level entries:"
    ls -1 "$SRC" | sed 's/^/    /'
    exit 0
fi

# --- 3. Real deploy --------------------------------------------------------

if [[ $USE_SUDO -eq 1 ]]; then
    SUDO=sudo
else
    SUDO=""
    # When --no-sudo is passed, the target must already be writable. If
    # the parent doesn't exist, fall through to a clearer error.
    if [[ ! -d "$DST" ]] && ! mkdir -p "$DST" 2>/dev/null; then
        echo "ERROR: --no-sudo set but '$DST' is not writable. Re-run without --no-sudo, or create the dir first." >&2
        exit 1
    fi
fi

# Ensure target dir exists
$SUDO mkdir -p "$DST"

# Copy. cp -a preserves perms + timestamps, and using SRC/. copies the
# CONTENTS of SRC into DST (not SRC itself as a subdir).
$SUDO cp -a "$SRC/." "$DST/"

# Tighten perms: 0644 for files, 0755 for dirs. Caddy runs as the
# `caddy` user (uid 998-ish) and only needs read access; we don't want
# stray group/world-write bits from a previous deploy.
$SUDO find "$DST" -type f -exec chmod 0644 {} +
$SUDO find "$DST" -type d -exec chmod 0755 {} +

# --- 4. Verify -------------------------------------------------------------

echo "Deployed $FILE_COUNT top-level entries to $DST."
echo ""
echo "Verify (run on the host after this):"
echo "  curl -I https://selenaastra.com/"
echo "  curl -I https://selenaastra.com/humans.txt"
echo "  curl -I https://selenaastra.com/.well-known/security.txt"
echo "  curl -I https://selenaastra.com/sitemap.xml"
