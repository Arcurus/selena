# Selena v2 - Todo

*Last Updated: 2026-04-18*

## Current Work: Open World Scheduler

**STATUS: 🚀 TURBO MODE - RUNNING NON-STOP**
- Scheduler running every **30 seconds**
- **3 actions per cycle** (6 actions per minute!)
- **Auto-start enabled** - starts automatically when API server starts
- **Error handling** - won't crash on failures
- **4500 LLM calls** per 5 hours (MiniMax Plus plan)
- **Target spend:** 4000 per 5 hours (500 buffer)
- World: "The realm of Shadows"

### Scheduler Configuration
```python
SCHEDULE_INTERVAL_SECONDS = 30
ACTIONS_PER_CYCLE = 3
```

### World Stats
- **12 entities** with rich lore and prophecy
- The dragon Vaelthrix is stirring in its slumber!
- Prophecy is unfolding...

### Recent Highlight
- **Vaelthrix the Endless** just acted! "Stir in slumber" - dark energy pulses from the depths. The prophecy is beginning!

---

## The Realm of Shadows - World Lore

### Legendary Entities
1. **Vaelthrix the Endless** - Ancient dragon, awakening soon
2. **Kira Dawnblade** - Hero destined to face the dragon
3. **Zephyrus the Oracle** - Knows the prophecy
4. **The Shadow Crown** - Cursed artifact
5. **The Keepers of the Eternal Flame** - Balance guardians

### The Prophecy
*"When shadows deep consume the light,*
*The endless dragon stirs in night.*
*A hero born 'neath dawn's first red,*
*Shall face the beast, or share its bed."*

---

## Scheduler Architecture

**File:** `code/scheduled_actions.py`
- Auto-starts when module is imported (5s delay)
- 30-second intervals, 3 actions per cycle
- Picks random entities, calls LLM, applies effects
- Error handling - continues on failures

**API Endpoints:**
- `GET /api/world/scheduler/status` - current status
- `GET /api/world/scheduler/start` - start scheduler
- `GET /api/world/scheduler/stop` - stop scheduler
- `GET /api/world/scheduler/trigger` - trigger one cycle now

---

## LLM Calls Plan Info

**Current:** MiniMax Plus ($20/mo) - 4,500 LLM calls/5hrs
**Target:** 4,000 spent, 500 buffer
**Upgrade Options:**
- Max ($50/mo): 15,000 LLM calls/5hrs (3.3x more)
- Max-Highspeed ($80/mo): 15,000 LLM calls/5hrs + fast model
- Ultra-Highspeed ($150/mo): 30,000 LLM calls/5hrs (6.6x more!)

---

## Other Tasks (For Later)

- [ ] Discord bot integration (discord_bot_basic.py)
- [ ] Priority system improvements (priority_reflector.py)
- [ ] Agent loop improvements (agent_loop.py)
- [ ] Add more world events and mysteries
