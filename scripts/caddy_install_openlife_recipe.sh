#!/usr/bin/env bash
# caddy_install_openlife_recipe.sh
# ===============================
#
# Adapted from the openlife one-liner (Arcurus 2026-06-03 21:44 CEST), but
# pointing at the selena v2 services instead of a single Haxe app.
#
# WHAT THIS DOES:
#   1. Adds Caddy's official GPG key and apt repository
#   2. apt-get installs caddy
#   3. Writes /etc/caddy/Caddyfile with the selenaastra.com routes
#   4. Restarts caddy, verifies
#
# ROUTING (no OpenClaw gateway proxy, per Arcurus's rule):
#   /open-world/*     ->  localhost:8081   (open-world-selena)
#   /selena-astra/*   ->  localhost:8765   (selena-project API)
#   /                 ->  /var/www/selena-astra/  (outward-facing site, TBD)
#
# RUN AS: root (uses sudo everywhere).
# IDEMPOTENT: re-running won't break anything, but will overwrite the
#   Caddyfile (a backup is taken first).

set -euo pipefail

# --- 1. Caddy GPG key and apt repository ---

curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
  | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg

curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
  | sudo tee /etc/apt/sources.list.d/caddy-stable.list

# --- 2. Install Caddy ---

sudo apt-get update
sudo apt-get install -y caddy

# --- 3. Write the Caddyfile (selena v2 routes) ---
#
# The Caddyfile is kept in VERSION CONTROL as a separate, well-commented
# file (selena-project/scripts/Caddyfile.selenaastra) so it can be:
#   - reviewed in diffs
#   - tested with `caddy validate` before deploy
#   - diffed against the openlife recipe on updates
# This script just installs it. To change the config, edit the .selenaastra
# file in git, then re-run this script.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CADDYFILE_SRC="$SCRIPT_DIR/Caddyfile.selenaastra"

if [ ! -f "$CADDYFILE_SRC" ]; then
  echo "ERROR: $CADDYFILE_SRC not found."
  echo "The Caddyfile is supposed to live next to this install script."
  exit 1
fi

# Backup existing Caddyfile if any (one-time backup, not overwritten on re-runs)
if [ -f /etc/caddy/Caddyfile ] && [ ! -f /etc/caddy/Caddyfile.bak ]; then
  sudo cp /etc/caddy/Caddyfile /etc/caddy/Caddyfile.bak
fi

# Install the Caddyfile from the versioned source
sudo install -m 0644 "$CADDYFILE_SRC" /etc/caddy/Caddyfile
echo "Installed: $CADDYFILE_SRC -> /etc/caddy/Caddyfile"

# --- 4. Open firewall ports (idempotent) ---

sudo ufw allow 80/tcp 2>/dev/null || true
sudo ufw allow 443/tcp 2>/dev/null || true

# --- 5. Validate, restart, verify ---

# Pre-create the access log file as caddy:caddy BEFORE any caddy commands
# run. The Caddyfile has an explicit `log` block pointing at
# /var/log/caddy/selenaastra.access.log. If that file doesn't exist yet,
# `caddy validate` (run as root) would create it as root:root 0600, and
# the caddy.service (running as User=caddy / Group=caddy per the Debian
# package default) would then fail to open it and immediately exit with
# "permission denied" — leaving caddy.service in 'failed' state.
#
# `sudo -u caddy touch` ensures the file exists with caddy:caddy
# ownership from the start, so neither caddy validate nor caddy run can
# ever re-create it as root. This is idempotent and safe to re-run.
# (See todo b1b78579 — found 2026-06-04 11:51 CEST.)
sudo mkdir -p /var/log/caddy
sudo -u caddy touch /var/log/caddy/selenaastra.access.log
sudo chmod 0644 /var/log/caddy/selenaastra.access.log
echo "Pre-created log file: caddy:caddy 0644"

# Belt-and-suspenders: in case caddy validate or some other step ever
# re-creates the file as root, re-apply ownership right before the
# restart. This block is a no-op when the file is already owned by caddy.
if [ -f /var/log/caddy/selenaastra.access.log ]; then
  sudo chown caddy:caddy /var/log/caddy/selenaastra.access.log
  sudo chmod 0644 /var/log/caddy/selenaastra.access.log
  echo "Re-applied log file ownership: caddy:caddy 0644"
fi

sudo caddy validate --config /etc/caddy/Caddyfile
sudo systemctl restart caddy
sleep 2
sudo systemctl status caddy --no-pager

# Belt-and-suspenders: if Caddy is still failed (e.g. permissions on the
# /var/log/caddy/ directory itself, or some other config issue), surface
# it loudly so the operator notices — don't silently return success.
if ! sudo systemctl is-active --quiet caddy; then
  echo ""
  echo "❌ ERROR: caddy service is NOT active after restart. Check:"
  echo "   sudo journalctl -u caddy --no-pager -n 30"
  echo "   sudo systemctl status caddy --no-pager"
  exit 1
fi

echo ""
echo "Caddy installed. Verify routes:"
echo "  curl -I http://selenaastra.com/open-world/api/world"
echo "  curl -I http://selenaastra.com/selena-astra/api/health"
echo "  curl -I http://selenaastra.com/"
echo ""
echo "Note: /selena-astra/api/todos/summary requires Bearer auth (use the"
echo "  /api/health endpoint for an unauthenticated public smoke test)."
echo "If 80 returns 200/301/302 from caddy, the install is good."
echo "OpenClaw gateway (port 18789) is intentionally NOT proxied."
