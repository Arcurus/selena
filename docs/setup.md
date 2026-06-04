# Selena v2 - Server Setup

## Services to Manage

### 1. Selena v2 API Server
- Port: 8765
- Runs: `python3 api_server.py`
- Should start on reboot

### 2. Open World Server (existing)
- Port: 8080
- Already configured

### 3. Caddy Reverse Proxy (CURRENT — replaced nginx 2026-06-03)
- Handles HTTPS (port 443) with **auto-issued, auto-renewed Let's Encrypt** certs
- Routes to appropriate backend based on path
- Caddyfile lives in version control: `selena-project/scripts/Caddyfile.selenaastra` (146 lines, well-commented)
- Install recipe: `selena-project/scripts/caddy_install_openlife_recipe.sh` (idempotent, 4 steps)
- Why Caddy over nginx: no certbot, no cron, no manual renewal, one-file config
- **Install is NOT YET RUN on production** — waiting on Arcurus to say "go" (sudo + service restart)
- Public hostnames: `selenaastra.com`, `www.selenaastra.com`
- Strict boundary: the OpenClaw gateway (port 18789) is intentionally NOT proxied

## Startup on Reboot

Using systemd service for auto-start:

```bash
# Create service file
sudo nano /etc/systemd/system/selena.service

# Service content:
[Unit]
Description=Selena v2 API Server
After=network.target

[Service]
Type=simple
User=openclaw
WorkingDirectory=/home/openclaw/openclaw/workspace/selena/code
ExecStart=/usr/bin/python3 api_server.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
# Enable and start
sudo systemctl enable selena
sudo systemctl start selena
```

## Current Heartbeat Monitoring

The current OpenClaw heartbeat should check if Selena is running:

```python
# In heartbeat, check:
import requests
try:
    response = requests.get('http://localhost:8765/api/status', timeout=2)
    if response.status_code == 200:
        print("Selena v2 is running")
    else:
        print("Selena v2 is responding but error")
except:
    print("Selena v2 is NOT running - need to restart")
```

## HTTPS Setup with Caddy (CURRENT)

**Note:** The old nginx section is preserved below for reference only — do not follow it. The production install will use Caddy, not nginx. Caddy auto-handles Let's Encrypt (no certbot, no manual renewal).

### 1. Run the install recipe (one-liner, idempotent)

```bash
sudo bash selena-project/scripts/caddy_install_openlife_recipe.sh
```

This will:
1. Add Caddy's official GPG key + apt repo
2. `apt-get install caddy`
3. Install `scripts/Caddyfile.selenaastra` → `/etc/caddy/Caddyfile` (146 lines, well-commented)
4. Open ports 80 + 443 in `ufw`
5. `caddy validate`, restart, and print status

### 2. Routing (already baked into the Caddyfile)

```
selenaastra.com, www.selenaastra.com
  /open-world/*   -->  http://localhost:8081/   (open-world-selena, Rust)
  /selena-astra/* -->  http://localhost:8765/   (selena-project API)
  /               -->  file_server /var/www/selena-astra/
```

`uri strip_prefix` rewrites the path on the way to the backend, so the open-world service sees `/api/world` rather than `/open-world/api/world`.

### 3. Verify

```bash
curl -I http://selenaastra.com/open-world/api/world     # 200 OK or 404 (no entity)
curl -I http://selenaastra.com/selena-astra/api/health  # 200 OK
curl -I http://selenaastra.com/                          # 200 OK (once outward site is deployed)
sudo systemctl status caddy                             # active (running)
```

### 4. Roll back (just in case)

```bash
sudo cp /etc/caddy/Caddyfile.bak /etc/caddy/Caddyfile  # if .bak exists from the install recipe
sudo systemctl restart caddy
```

---

## Old nginx section (REFERENCE ONLY — superseded by Caddy above)

> Do not follow this. Kept for historical context. The Caddy swap happened 2026-06-03 (openlife Caddy one-liner recipe, adapted for selena v2 by Arcurus).

### 1. Install Certbot
```bash
sudo apt update
sudo apt install certbot python3-certbot-nginx
```

### 2. Nginx Configuration for Selena
```nginx
server {
    listen 80;
    server_name selena.yourdomain.com;

    location / {
        proxy_pass http://127.0.0.1:8765;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

### 3. Get SSL Certificate
```bash
sudo certbot --nginx -d selena.yourdomain.com
```

## Webhook/Reverse Proxy Setup (REFERENCE ONLY — now in Caddyfile.selenaastra)

### Open World
```nginx
location /openworld/ {
    proxy_pass http://127.0.0.1:8080/;
    proxy_set_header Host $host;
}
```

### Selena
```nginx
location /selena/ {
    proxy_pass http://127.0.0.1:8765/;
    proxy_set_header Host $host;
}
```

## Hooks System

Selena can add hooks to extend functionality:

### Hook Types
- `webhook`: External HTTP callbacks
- `cron`: Scheduled tasks
- `event`: Event-triggered actions

### Hook Storage
```
selena/
├── hooks/
│   ├── webhook/
│   │   └── {hook_name}.json
│   ├── cron/
│   │   └── {hook_name}.json
│   └── event/
│       └── {hook_name}.json
```

### Hook Format
```json
{
    "name": "github_notify",
    "type": "webhook",
    "url": "https://api.github.com/...",
    "trigger": "task_completed",
    "enabled": true
}
```

## Service Health Checks

Selena should monitor:
1. Her own API server
2. Open World server
3. Caddy (was: Nginx) — only relevant once Caddy is installed
4. Any other critical services

```python
def check_services():
    services = {
        'selena': 'http://localhost:8765/api/status',
        'openworld': 'http://localhost:8081/health',   # port 8081 (was 8080, see 2026-04-19 history)
        'caddy': 'https://selenaastra.com/selena-astra/api/health',  # post-Caddy-install
    }
    
    results = {}
    for name, url in services.items():
        try:
            r = requests.get(url, timeout=2)
            results[name] = 'ok' if r.status_code == 200 else 'error'
        except:
            results[name] = 'down'
    
    return results
```

---

*Setup documentation for Selena v2*
