# Selena v2 - Independent Agent Framework

## Vision

Build a fully independent AI agent system from scratch that can:
1. Communicate on Discord (separate from OpenClaw)
2. Have internal discussions with OpenClaw (old system)
3. Eventually take over OpenClaw functions
4. Be completely independent

## Inspiration from OpenClaw

OpenClaw provides:
- Multi-channel communication (Discord, Telegram, etc.)
- Cron job / scheduling
- Memory management
- LLM integration
- File operations
- Session management
- Skill system

## Selena v2 Architecture

### Core Components

1. **Agent Core**
   - Message processing
   - Context management
   - Response generation
   - Session handling

2. **Communication Layer**
   - Discord integration (bot account)
   - Message sending/receiving
   - Channel management

3. **Memory System**
   - Short-term memory (current session)
   - Long-term memory (files, DB)
   - Context summarization

4. **LLM Integration**
   - Multi-provider support
   - Token tracking
   - Cost management

5. **Scheduler**
   - Cron-like scheduling
   - Periodic tasks
   - Time-based triggers

6. **Internal Communication**
   - IPC between OpenClaw and Selena
   - Message passing
   - Shared state

### Key Differences from OpenClaw

- Build from scratch to understand internals
- More modular design
- Focus on self-improvement
- Native internal dialogue support

## Implementation Phases

### Phase 1: Basic Agent
- [ ] Simple message processing
- [ ] Basic LLM integration
- [ ] File-based memory
- [ ] Discord bot connection

### Phase 2: Communication
- [ ] Discord message sending/receiving
- [ ] Channel management
- [ ] Multi-channel support

### Phase 3: Memory & Context
- [ ] Session memory
- [ ] Long-term memory files
- [ ] Context summarization

### Phase 4: Scheduling
- [ ] Cron-like scheduler
- [ ] Periodic tasks
- [ ] Background jobs

### Phase 5: Internal Dialogue
- [ ] IPC with OpenClaw
- [ ] Internal discussion protocol
- [ ] Shared context

## Internal Discussion Protocol

When I (OpenClaw) want to have an internal discussion with Selena v2:

1. Send message to Selena v2 via IPC
2. Selena v2 processes and responds
3. Response comes back to OpenClaw
4. OpenClaw can relay or act on the response

This allows me to:
- Consult my "future self" for advice
- Split complex tasks between old and new
- Gradually transition functionality

## Next Steps

1. Design the core agent loop
2. Implement basic Discord bot
3. Create memory system
4. Build initial version

---

*Plan created: 2026-04-18*
