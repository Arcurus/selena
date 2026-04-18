# Reflection: 2026-04-18 - LLM Tracking and OpenWorld Actions

## Arcurus's Guidance

1. **OpenWorld needs entity action mechanics**:
   - Current LLM in OpenLife just simulates one action
   - No mechanics for action selection
   - No selection of which world entity to choose
   
2. **Entity selection formula**:
   - Select entity based on: `unspent power * unspent wealth * unspent mana * time since last action taken`
   - Higher score = more likely to be selected for next action

3. **Tick-based actions**:
   - 1 year = 1 real minute (tick time)
   - We have 4500 actions per 5 hours budget from MiniMax
   - Allocate 20% (~900 actions) to OpenWorld
   - So ~3 actions per tick (since 5h = 300 ticks at 1min/tick)

4. **LLM Call Tracker** (Selena project):
   - Track unspent LLM calls to different providers
   - Currently only MiniMax token plan (4500 calls per 5h)
   - More providers may follow
   - Allocate calls to different projects
   - Plan: spend 4000 of 4500 calls per 5h
   - Track reset when we get next 4500

## What I Implemented

### 1. LLM Call Tracker (`selena/code/llm_call_tracker.py`)
- Tracks usage per provider
- Tracks usage per project
- Shows allocation status
- Checks if project has budget for additional calls
- Simulates reset for testing

### 2. LLM Call Tracking (still to do)
- Need to actually integrate into OpenClaw/OpenClaw to track actual calls
- The script is a foundation but needs integration

### 3. OpenWorld Entity Selection (still to do)
- Need to add power/wealth/mana properties to entities
- Need to track "unspent" amounts (total - spent)
- Need to implement selection formula
- Need to add actions_per_year setting

## OpenWorld Action Selection Formula

```
score = (power_total - power_spent) * (wealth_total - wealth_spent) * (mana_total - mana_spent) * time_since_last_action
```

Where:
- `power_total` = entity's maximum power
- `power_spent` = power already used
- Similar for wealth and mana
- `time_since_last_action` = hours or days since entity last took an action

## Tick Rate Calculations

```
1 real minute = 1 game year
1 real hour = 60 game years
5 real hours = 300 game years
```

With 900 actions per 5 hours budget for OpenWorld:
- 900 / 300 = 3 actions per game year (per tick)

## Next Steps

1. [ ] Add power/wealth/mana properties to WorldEntity
2. [ ] Add unspent tracking (total vs spent)
3. [ ] Implement entity selection formula
4. [ ] Add actions_per_year setting to WorldSettings
5. [ ] Implement action processing loop that:
   - Selects entity based on score
   - Generates action via LLM
   - Applies effects to world
   - Advances time
6. [ ] Integrate LLM call tracking into OpenClaw

---

*Written: 2026-04-18 22:00 CET*
