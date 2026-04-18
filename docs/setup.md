# Selena v2 - Server Setup

## Services to Manage

### 1. Selena v2 API Server
- Port: 8765
- Runs: `python3 api_server.py`
- Should start on reboot

### 2. Open World Server (existing)
- Port: 8080
- Already configured

### 3. Nginx Reverse Proxy
- Handles HTTPS (port 443)
- Routes to appropriate backend based on domain/path

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

## HTTPS Setup with Nginx

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

## Webhook/Reverse Proxy Setup

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
3. Nginx
4. Any other critical services

```python
def check_services():
    services = {
        'selena': 'http://localhost:8765/api/status',
        'openworld': 'http://localhost:8080/health',
        'nginx': 'http://localhost/health'
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
