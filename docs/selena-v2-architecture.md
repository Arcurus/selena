# Selena v2 - Full Architecture

## Core Concept

Selena v2 is a self-contained AI agent with its own memory, heartbeat, and development loop. It can operate independently and eventually take over functions from OpenClaw.

## Memory Hierarchy

### 1. Soul Layer (Core Identity)
- **soul.md** - Core identity, values, purpose
- **personality.md** - Traits, communication style
- **guidelines.md** - Operating principles

### 2. Agent Layer (Self-Model)
- **agent.md** - Current state, capabilities
- **skills.md** - Available skills and when to use them
- **tools.md** - Tool descriptions and usage

### 3. Heartbeat Layer (Self-Management)
- **heartbeat.md** - Self-check instructions, priorities
- **health.md** - System status, resource usage
- **goals.md** - Current goals and progress

### 4. Global Memory (Long-term)
- **memory/global/** - Shared knowledge across all sessions
- **memory/daily/** - Daily logs and notes
- **memory/projects/** - Project-specific knowledge
- **memory/reflections/** - Learned insights

### 5. Project Memory (Per-Project)
- **projects/{name}/context/** - Project-specific context
- **projects/{name}/status.md** - Current project status
- **projects/{name}/plans/** - Project plans

## Context Loop (Similar to Open World World Action)

The core agent loop follows the same pattern as Open World world actions:

```
┌─────────────────────────────────────┐
│  1. CONTEXT (Gather)                │
│     - Query relevant memories       │
│     - Load current state            │
│     - Get pending tasks/heartbeats   │
└─────────────────┬───────────────────┘
                  │
                  ▼
┌─────────────────────────────────────┐
│  2. CALL (LLM)                      │
│     - Build prompt from context      │
│     - Make LLM call                 │
│     - Track LLM calls/costs           │
└─────────────────┬───────────────────┘
                  │
                  ▼
┌─────────────────────────────────────┐
│  3. PROCESS (Execute)               │
│     - Parse response                 │
│     - Execute actions                │
│     - Update memory                 │
│     - Schedule follow-up if needed   │
└─────────────────┬───────────────────┘
                  │
                  ▼
         ┌────────┴────────┐
         │ Need more?      │
         │ Loop if yes     │
         └─────────────────┘
```

### Context Phase
- Query global memory for relevant information
- Load project-specific context
- Get current heartbeat tasks
- Gather recent reflections/learnings

### Call Phase
- Build comprehensive prompt
- Include soul/agent context
- Add relevant memories
- Track LLM calls and costs

### Process Phase
- Parse LLM response
- Execute planned actions
- Update relevant memories
- Determine if more calls needed

## Debug Interface

Like Open World has debug for world actions, Selena v2 should have:

### Web Interface
- View current context
- See recent LLM calls
- Monitor memory usage
- View pending tasks

### APIs
- `GET /context` - Current context state
- `GET /memory/search?q=...` - Search memories
- `POST /memory` - Add to memory
- `GET /heartbeat/status` - Heartbeat status
- `POST /heartbeat/task` - Add task
- `GET /llm/calls` - LLM call history

## Implementation Phases

### Phase 1: Basic Agent Loop
- [ ] Core memory system (soul, agent, heartbeat)
- [ ] Simple context -> call -> process loop
- [ ] Basic file-based storage

### Phase 2: Memory Intelligence
- [ ] Relevance-based context retrieval
- [ ] Automatic memory prioritization
- [ ] Learning from interactions

### Phase 3: Self-Management
- [ ] Own heartbeat system
- [ ] Priority-based task management
- [ ] Resource monitoring

### Phase 4: Debug & Interface
- [ ] Web interface
- [ ] Debug APIs
- [ ] Memory visualization

## Comparison with Open World

| Open World | Selena v2 |
|------------|-----------|
| World Entity | Self (Soul/Agent) |
| World Action | Agent Task |
| Entity Memory | Project Memory |
| Global Context | Global Memory |
| LLM Call | LLM Call |
| Action Processing | Task Processing |

## Example: Development Loop

```
1. CONTEXT
   - Check heartbeat tasks
   - Load relevant project memories
   - Get recent reflections

2. CALL
   - "Based on pending tasks and recent context, what should I work on next?"

3. PROCESS
   - Execute task
   - Update project status
   - Add reflection
   - If task incomplete, loop
```

---

*Plan created: 2026-04-18*
