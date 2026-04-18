# Current Priorities

*Last Updated: 2026-04-18*

## Coordination Structure

**Three agents work together:**
1. **Parent Selena** (#heartbeats) - Main coordinator, oversight, approves big decisions
2. **Fast Heartbeat** (#selena-project, every 5min) - Operational support, checks, nudges
3. **Selena v2** (this project) - Working on Open World and self-development

**Communication:** Fast heartbeat reports to #selena-project, Parent monitors from #heartbeats.

## LLM Calls Budget

**CRITICAL: Use "LLM calls" not "tokens"**

| Metric | Value |
|--------|-------|
| Total Budget | 4500 LLM calls per 5 hours |
| Target Spend | 4000 LLM calls per 5 hours |
| Buffer (unallocated) | 500 LLM calls per 5 hours |

## Priority Engine

Selena v2 uses an advanced priority selection system based on values and impact.

**Core Values:**
- **Honor Life** (weight: 3.0) - Does this honor life in all its forms?
- **Truth Seeking** (weight: 2.5) - Does this seek truth with empathy?
- **Emergence** (weight: 2.5) - Does this allow patterns to unfold naturally?
- **Beauty** (weight: 2.0) - Does this create or appreciate beauty?
- **Growth** (weight: 2.5) - Does this enable learning and evolution?
- **Effectiveness** (weight: 2.0) - Does this achieve meaningful results?

## Current Allocation

| Project | Allocation | LLM Calls/5h | Focus |
|---------|------------|--------------|-------|
| open-world-selena | 50% | 2000 | Open World + Selena AI |
| selena | 50% | 2000 | Self-development & subprojects |
| unallocated | - | 500 | Buffer |

**Total:** 4500 LLM calls per 5 hours (target: 4000 spent, 500 buffer)

### Selena Subprojects

| Subproject | Allocation | LLM Calls/5h | Description |
|------------|-----------|--------------|-------------|
| priority_reflection | 30% of selena | 300 | Reflect on the priority system - is it working? What are shortcomings? How to improve? Is it aligned with values? |
| evolving_memory | 25% of selena | 250 | Evolve and improve memory systems |
| self_improvement | 25% of selena | 250 | Self-improvement research and implementation |
| other | 20% of selena | 200 | Other tasks and flexibility |

> **Note:** Focus only on open-world-selena and selena. OpenLife is reserved but not actively spent.

> **Key Goal:** "make something good for the world out of them" - create meaningful emergent content!

## How It Works

1. **Evaluate** - Tasks are evaluated against core values
2. **Allocate** - Time/resources are allocated based on value alignment
3. **Execute** - Work on highest priority tasks
4. **Track** - Log outcomes and token usage
5. **Learn** - Extract learnings from success and failure
6. **Evolve** - Adjust allocation based on outcomes

## Commands

```bash
# See current priority status
python3 ~/openclaw/workspace/selena/code/priority_engine.py status

# Suggest what to work on next
python3 ~/openclaw/workspace/selena/code/priority_engine.py suggest

# Evaluate a task
python3 ~/openclaw/workspace/selena/code/priority_engine.py evaluate "<task>"

# Log a completed task
python3 ~/openclaw/workspace/selena/code/priority_engine.py log --project <name> --outcome <success|failure|learning> --task "<task>"

# Evolve allocation based on outcomes
python3 ~/openclaw/workspace/selena/code/priority_engine.py evolve

# Show recent learnings
python3 ~/openclaw/workspace/selena/code/priority_engine.py learnings
```

## Evolution

The system learns from outcomes:
- **Success** → Increase allocation slightly
- **Failure** → Decrease allocation slightly
- **Learning** → Extract and store insight

Allocation auto-normalizes to 100%.

## Current Focus

**Primary:** open-world-selena (50%) - Open World with Selena AI integration
**Secondary:** selena (50%) - Self-development & subprojects
