# Selena Project - Projects

*Last Updated: 2026-06-09*

## Active Projects

### open-world-selena (PARENT)
- **name**: open-world-selena
- **Status**: active
- **Description**: PARENT. The Rust open-world simulation as a whole. Per Arcurus 2026-06-09 #cost-tracker, this project is split into two children for cost attribution. The parent is a rollup — no events land here directly.
- **Port**: 8081
- **Repo**: https://github.com/Arcurus/openworld-selena
- **Priority**: 9
- **Tech**: Rust, Axum, OpenAI
- **is_parent**: true
- **children**: open-world-dev, open-world-running
- **Features**: Entity actions, world events, power tiers, nearby entities
- **auto_start**: true
- **enabled**: true
- **check_method**: http
- **health_url**: http://127.0.0.1:8081/api/world/stats
- **start_command**: systemctl --user restart open-world-selena.service
- **grace_period_seconds**: 60
- **max_restarts_per_hour**: 4

### orchestrator-status
- **name**: orchestrator-status
- **Type**: service
- **Status**: active
- **Description**: Read-only HTTP status API for the lunar UI (selena-project-2/orchestrator/orchestrator_status.py). Lives in selena-project-2 but tracked here so selena-api's service_manager can auto-heal it. Added 2026-06-11 per Arcurus #selena-project: "all the other services can be tracked in project selena right? so a service can be registered there as auto start that we all added already".
- **Port**: 8766
- **Repo**: https://github.com/Arcurus/selena-project-2
- **auto_start**: true
- **enabled**: true
- **check_method**: http
- **health_url**: http://127.0.0.1:8766/health
- **start_command**: systemctl --user restart orchestrator-status.service
- **grace_period_seconds**: 60
- **max_restarts_per_hour**: 4

### open-world-dev (CHILD)
- **name**: open-world-dev
- **Status**: active
- **Description**: CHILD of open-world-selena. OpenClaw-side dev work for the world simulation: the selena-open-world-worker cron (job 13b68e52) that monitors the world, generates digests, posts to #openworld + #openworld-log. LLM calls made by that worker land here. Does NOT include LLM calls generated inside the running simulation (those go to open-world-running).
- **parentProject**: open-world-selena
- **Tech**: Python (workers), OpenClaw
- **Features**: Worker cron 13b68e52, Discord channels #openworld + #openworld-log

### open-world-running (CHILD)
- **name**: open-world-running
- **Status**: active
- **Description**: CHILD of open-world-selena. LLM calls made by the LIVE open-world simulation (the Rust binary at record_llm_call_async in main.rs). Every entity action that triggers a model call is tagged with project=open-world-running. The Rust binary reads the project name from the OW_LLM_PROJECT env var (default: open-world-running).
- **parentProject**: open-world-selena
- **Tech**: Rust, Axum
- **Features**: Tagged at the model-call site in the Rust binary

### selena-project
- **name**: selena-project
- **Status**: active
- **Description**: Self-development, memory, reflection system for Selena v2
- **Port**: 8765
- **Repo**: https://github.com/Arcurus/selena
- **Priority**: 9
- **Tech**: Python, Flask, Knowledge Base, Todo System
- **Features**: API server, web UI, self-evolution loop
- **auto_start**: false  # selena-api heals itself; the external watchdog (api-health-watchdog) is its safety net. Flipping this to true would create a circular self-restart.
- **enabled**: true
- **check_method**: http
- **health_url**: http://127.0.0.1:8765/api/health
- **start_command**: systemctl --user restart selena-project.service
- **grace_period_seconds**: 60
- **max_restarts_per_hour**: 4

### OpenLife
- **name**: OpenLife
- **Status**: active
- **Description**: AI-driven life simulation with autonomous NPCs
- **Port**: 8000
- **Repo**: https://github.com/Arcurus/open-life-editor
- **Tech**: Haxe, JavaScript
- **Features**: 50 AI NPCs, autonomous villages, dynamic events

### Selena
- **name**: Selena
- **Status**: active
- **Description**: Self-development: research, planning, execution, reflection, optimization
- **Tech**: Python
- **Features**: Child agent, self-improvement loop

### OHOL Editor
- **name**: OHOL Editor
- **Status**: active
- **Description**: Web editor for OneLife object files with sprite preview
- **Tech**: Python, HTML5 Canvas
- **Features**: Parse .txt object files, binary cache, TGA sprite preview, zoom 25%-400%
- **Directory**: ohol-editor
- **Repo**: https://github.com/Arcurus/open-life-editor

### llm-loop-example
- **name**: llm-loop-example
- **Status**: experimental
- **Description**: Rust example of LLM loop patterns
- **Tech**: Rust, Cargo
- **Features**: LLM integration examples

## Archived Projects

### open-world (original)
- **name**: open-world (original)
- **Status**: archived
- **Reason**: Replaced by open-world-selena with better architecture
- **Repo**: (old repo, no longer maintained)

## Project Properties

Each project should track:
- name: Project name
- description: What the project does
- status: active, paused, archived
- port: Main port (if applicable)
- repo: GitHub repository URL
- priority: 1-10 priority
- tech: Main technologies
- features: Key features list
- lastUpdated: Last update timestamp
