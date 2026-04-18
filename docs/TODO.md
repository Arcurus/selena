# Selena v2 - TODO

## Priority 0: LLM Calls Management (CRITICAL)

### LLM Calls Awareness
- [x] Cost tracking in web interface (LLM Calls Plan tab)
- [x] LLM Provider tracking (per provider usage)
- [x] **LLM calls budget allocation per project**
  - MiniMax: 4500 calls/5h, target spend 4000 (500 buffer)
  - Allocations via priority_engine.py:
    - open-world-selena 50% (2000 LLM calls/5h)
    - selena 50% (2000 LLM calls/5h)
    - unallocated 500 (buffer)
- [x] **Wise LLM call allocation based on project priority**
- [x] **Daily LLM calls budget monitoring and alerts** (in heartbeat cron job)
- [x] **Auto-adjust work based on remaining LLM calls**
- [x] **Track OpenWorld vs Selena vs other project usage** (via llm_call_tracker.py)
- [ ] **Get real data from MiniMax token plan API**

### LLM Calls Budget (UPDATED: 2026-04-18)
- **MiniMax Token Plan: 4500 LLM calls per 5 hours**
- **Target Spend: 4000 per 5 hours (500 buffer unallocated)**
- **Allocate less when actively processing Arcurus input**
- Need to track per-project usage
- Need to be LLM call-conscious in all decisions

---

## Priority 1: Core Infrastructure

### 1.1 Startup & Auto-Restart
- [ ] Create systemd service for Selena v2 API server
- [ ] Enable on reboot
- [ ] Test that it starts correctly after system restart

### 1.2 Heartbeat Monitoring
- [ ] Update current OpenClaw heartbeat to check Selena v2 status
- [x] Create faster heartbeat for Selena v2 (for programming/follow-ups)
  - Reports to #selena-project channel (ID: 1495170712397152367)
  - Runs every 5 minutes, checks servers and works on TODO
- [ ] Monitor: API server, Shiba workers, Open World, Nginx

### 1.3 HTTPS Setup
- [ ] Check nginx configuration on VPS
- [ ] Create nginx site configuration for Selena v2
- [ ] Set up SSL certificate (Let's Encrypt)
- [ ] Configure reverse proxy

### 1.4 Service Monitoring in Web Interface
- [ ] Real service status from API instead of mock data
- [ ] Connect to actual running services
- [ ] Show real port, uptime, last seen data

## Priority 2: Integration

### 2.1 Open World Integration
- [ ] Add Open World to services panel
- [ ] Add webhook forwarding in nginx
- [ ] Connect Open World status to Selena v2 dashboard

### 2.2 Hooks System
- [ ] Design hook structure (webhook, cron, event)
- [ ] Create hooks directory and storage
- [ ] Implement basic hook execution

### 2.3 Sub-Worker System
- [ ] Shiba Miner implementation (context mining)
- [ ] Failure reporting to main Selena
- [ ] Task queue for workers

## Priority 3: Web Interface Improvements

### 3.1 Real Data
- [ ] Connect API server to real data sources
- [ ] Real cost tracking from providers
- [ ] Real activity log from system events

### 3.2 User Experience
- [ ] Improve graphs and visualization
- [ ] Add notifications/alerts
- [ ] Mobile responsive design

## Priority 4: Documentation

- [ ] Update setup.md with actual steps
- [ ] Create API documentation
- [ ] Document hook system

---

## Status: 2026-04-18

### Completed:
- API server created (selena/code/api_server.py)
- Web interface created (selena/web/index.html) with:
  - Services panel
  - Cost tracking (Token Plan, LLM Providers)
  - Activity & Error logging with timeline
  - Color-coded status (Green=Waiting, Yellow=Issued, Blue=Working, Red=Broken)
  - Settings for port configuration
- Documentation structure (selena/docs/)

### In Progress:
- Testing API server
- Implementing real service monitoring

### Blocked:
- None
