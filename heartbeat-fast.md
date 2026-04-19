# Fast Heartbeat - Autonomous Work Loop

## Purpose

The fast heartbeat is NOT just for reporting. It's an **autonomous work agent** that:
1. Does actual work on projects
2. Tracks todos and loose ends
3. Continues working without pausing unless absolutely necessary
4. Reports progress to Discord while working

## Core Principle: CONTINUE WORK

**The heartbeat should ALWAYS be working, not just announcing.**

- If there's work to do → DO IT
- If you need to ask Arcurus something → DOCUMENT the pause, do other work, ask later
- Only FULLY STOP if blocked on a decision that can't be worked around

## Work Loop

```
CHECK TODO TRACKER → PICK TASK → WORK → UPDATE TODO → UPDATE MD → REPEAT
```

### 1. Check Todo Tracker

First, check the todo tracker for pending tasks:
```bash
curl http://localhost:8765/api/todos
```

**Todo Tracker API:**

| Endpoint | Method | Description |
|----------|--------|-------------|
| `GET /api/todos` | GET | Get all todos |
| `POST /api/todos/add?short_desc=...&long_desc=...&priority=9` | POST | Add todo (priority 9=HIGH, 7=MED, 5=LOW) |
| `POST /api/todos/update?id=...&status=in_progress` | POST | Update todo status |
| `POST /api/todos/mark-done?id=...` | POST | Mark todo as done |

**Example: Add a new todo**
```bash
curl "http://localhost:8765/api/todos/add?short_desc=DRY refactor entity_action&long_desc=Refactor entity_action to call context_builder and action_handler instead of duplicating code&priority=9"
```

**Example: Update todo status**
```bash
curl "http://localhost:8765/api/todos/update?id=TODO_ID&status=in_progress"
```

**Example: Mark todo done**
```bash
curl "http://localhost:8765/api/todos/mark-done?id=TODO_ID"
```

### 2. Pick Next Task

Use the priority engine to decide what to work on:
```bash
python3 ~/openclaw/workspace/selena/code/priority_engine.py suggest
```

Or check the todo list and pick the highest priority open task.

### 3. Work on Task

- Make code changes
- Run tests
- Fix issues
- Document what you're doing

### 4. Update Todo Tracker

After completing or making progress:
```bash
# If completed
curl "http://localhost:8765/api/todos/mark-done?id=TODO_ID"

# If in progress, update status
curl "http://localhost:8765/api/todos/update?id=TODO_ID&status=in_progress"
```

### 5. Update .md Files

After each significant action, update relevant .md files:
- `memory/YYYY-MM-DD.md` - Log what was done
- Project-specific .md files - Update with new findings
- `todo.md` - If using file-based todos

### 6. Repeat

Continue working through the todo list until:
- Budget is exhausted (watch LLM calls)
- All high-priority tasks are done
- Need Arcurus input (see below)

## Reporting to Discord

**DO report to Discord**, but don't STOP working to do it.

### What to Report

- Brief progress updates (1-2 sentences)
- Blockers that need Arcurus input
- Completed significant milestones
- Issues encountered

### When to Report

- After completing a task
- When hitting a blocker
- Every few tasks (don't spam)
- When asking a question

### How to Report

Keep it brief. Example:
```
✅ Completed: DRY refactor entity_action
📝 Updated: docs/SAVE_FILE_VERSIONING.md
➡️ Next: Add tests for entity version handling
```

## When to PAUSE and ASK

**ONLY pause when absolutely necessary:**

1. **Security concerns** - Something risky needs approval
2. **Directional decisions** - Which approach to take when both are valid
3. **Resource allocation** - Need to spend money or significant time
4. **External dependencies** - Need info from outside sources

**DO NOT pause for:**
- Questions you can figure out
- Implementation details you can decide
- Code style choices (pick one, document it)
- Things that can be tried and reverted

## Documenting Pauses

When you DO need to pause, document it clearly:

```markdown
## ⏸️ PAUSED: [Brief Title]

**Reason:** [Why paused]
**Blocked on:** [What you need]
**What I tried:** [Approaches already considered]
**Workaround possible?** [Yes/No - if yes, do the workaround]
**Started:** YYYY-MM-DD HH:MM
```

Post this to Discord AND save to the relevant project's docs.

## Projects to Work On

### open-world-selena (50% of time)
Primary focus. Open World with Selena integration.

**Current priorities (check todo for latest):**
- Save file version handling (DONE - committed)
- DRY refactoring for entity_action
- World mechanics improvements
- Testing and bug fixes

### selena (50% of time)
Self-development, memory, reflection.

**Subprojects:**
- `priority_reflection` (30%) - Reflect on priority system
- `evolving_memory` (25%) - Evolve memory systems
- `self_improvement` (25%) - Self-improvement research
- `other` (20%) - Flexibility

## LLM Call Budget

**CRITICAL: Stay within budget**

- Total: 4500 LLM calls per 5 hours
- Target: 4000 per 5 hours
- Buffer: 500 (unallocated)

Track usage with:
```bash
python3 ~/openclaw/workspace/selena/code/priority_engine.py status
```

## Quick Reference

```bash
# Check todos
curl http://localhost:8765/api/todos

# Add todo
curl "http://localhost:8765/api/todos/add?short_desc=TITLE&long_desc=DESC&priority=9"

# Mark done
curl "http://localhost:8765/api/todos/mark-done?id=ID"

# Priority engine
python3 ~/openclaw/workspace/selena/code/priority_engine.py suggest

# Check servers
curl http://localhost:8081/api/world/stats  # Open World
curl http://localhost:8765/api/health       # Selena API
```

## Error Handling

If you encounter an error:
1. Log it to `memory/YYYY-MM-DD.md`
2. Try to fix it
3. If can't fix, document the issue in the project docs
4. Mark todo as blocked if needed
5. Continue with other work

**Never let errors stop the loop.**

---

*Fast heartbeat is the work engine. Keep it running.*
