# Selena v2 - Visualization System

## Concept

A web interface that visualizes different agents/workers of Selena v2 working on various tasks. Uses generated images to create a visual representation of what's happening.

**Visual Theme:**
- Selena as an overseeing AI presence (lunar/celestial theme)
- Shiba Inus as workers/miners doing the work
- Transparent view of context, calls, processing

## Web Interface

### Main Dashboard
```
┌─────────────────────────────────────────────────────────────┐
│  🌙 SELENA v2 - Agent Visualization                        │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  ┌─────────────────────────────────────────────────────┐   │
│  │  👩‍💻 SELENA (Overseer)                                  │   │
│  │  Status: Active | Tasks: 5 | Context: 80%          │   │
│  └─────────────────────────────────────────────────────┘   │
│                                                             │
│  ┌─────────┐ ┌─────────┐ ┌─────────┐                     │
│  │ 🐕 Miner│ │ 🐕 Miner│ │ 🐕 Coder│                     │
│  │ Status  │ │ Status  │ │ Status  │                     │
│  │ Mining  │ │ Mining  │ │ Working │                     │
│  │ Progress│ │ Progress│ │ Progress│                     │
│  └─────────┘ └─────────┘ └─────────┘                     │
│                                                             │
│  ┌─────────────────────────────────────────────────────┐   │
│  │  📊 Current Action                                  │   │
│  │  Context: Loading... | Call: Processing...          │   │
│  └─────────────────────────────────────────────────────┘   │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

### Agent Cards
Each agent shows:
- Name/role
- Current task
- Progress/status
- Recent activity

### Visualization Images

Using MiniMax CLI for image generation:

**Selena Overseer:**
- "Celestial AI presence, lunar goddess overseeing workers, ethereal light"
- Style: Digital art, ethereal, cosmic

**Shiba Workers:**
- "Cute Shiba Inu mining cryptocurrency, underground mine, pixel art"
- "Shiba Inu coding on computer, cozy workspace, illustration"
- "Shiba Inu writing in journal, study room, warm lighting"

## Implementation

### 1. Web Interface (HTML/CSS/JS)
- Simple single-page app
- Shows agent cards
- Real-time updates via polling or WebSocket
- Displays current context/call/process state

### 2. Agent Status API
```
GET /api/agents
GET /api/agents/{id}/status
GET /api/current-action
```

### 3. Image Generation
```bash
minimax-img-gen "prompt" --output agent.png
```

### 4. MiniMax CLI Integration
```python
def generate_agent_image(agent_type: str, style: str = "digital") -> str:
    prompts = {
        "selena": "Celestial AI presence, lunar goddess...",
        "shiba_miner": "Cute Shiba Inu mining...",
        "shiba_coder": "Shiba Inu coding...",
    }
    return minimax_cli.generate(prompts[agent_type])
```

## File Structure

```
selena/
├── web/
│   ├── index.html      # Main dashboard
│   ├── style.css       # Styling
│   ├── app.js          # Frontend logic
│   └── agents/         # Generated agent images
├── code/
│   ├── agent_loop.py   # Main agent loop
│   ├── api_server.py   # Status API
│   └── image_gen.py    # MiniMax image generation
└── docs/
    └── visualization.md
```

## MiniMax CLI Command

```bash
minimax-img-gen "A cute Shiba Inu wearing a hardhat, mining in a tunnel, digital art" -o shiba_miner.png
```

## Next Steps

1. Create web interface structure
2. Add agent status tracking
3. Integrate MiniMax CLI for image generation
4. Create visualization dashboard

---

*Visualizing AI work through cute Shiba Inus! 🐕*
