# Running Services Tracker

**Last Updated:** 2026-04-19 08:52 CEST

## Active Services

| Service | PID | Port | Status | Description | Binary Path |
|---------|-----|------|--------|-------------|-------------|
| open-world-selena | 39055 | 8081 | ✅ Running | Dynamic evolving world simulation with LLM-driven entities | /home/openclaw/openclaw/workspace/open-world-selena/target/release/open_world |
| selena-project API | 27884 | 8765 | ✅ Running | Selena v2 API server with Knowledge Base and Web UI | /home/openclaw/openclaw/workspace/selena-project/code/api_server.py |

## Service Details

### open-world-selena
- **Purpose:** LLM-driven dynamic world simulation
- **World Name:** The Realm of Shadows
- **Started:** 2026-04-19 08:52 CEST
- **Save File:** Fresh world (no entities yet - save file was cleared due to format mismatch)
- **Features:** Entity history, nearby entities, power tier context, world events
- **API:** REST API on port 8080

### selena-project API  
- **Purpose:** Selena v2 self-development API
- **Started:** 2026-04-19 01:27 CEST
- **Features:** Knowledge Base System, Todo System, Self-Evolution Loop
- **Web UI:** http://localhost:8765/

## Historical Notes

- **2026-04-19 09:02:** open-world-selena moved to port 8081 (was 8080). Fresh world started.
- **2026-04-19 08:52:** Restarted open-world-selena after binary rebuild. Old save file deleted due to format mismatch with new entity structures (power_tier, entity_history, nearby_entities).
- **2026-04-19 01:27:** selena-project API server started via cron job
- **2026-04-18 23:19:** Last heartbeat before overnight gap
- **2026-04-18 16:48:** Old open-world server started (was running until 08:52)

## How to Check Service Status

```bash
# Check if server is running
curl -s http://localhost:8080/api/world/stats
curl -s http://localhost:8765/api/status

# Check processes
ps aux | grep open_world
ps aux | grep api_server
```

## Auto-Start

Currently no systemd services configured. Servers are started manually or via cron jobs.

## TODO
- [ ] Implement systemd service files for auto-start on boot
- [ ] Add health check endpoints to all services
- [ ] Create monitoring cron job to track service status
