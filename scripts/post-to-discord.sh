#!/bin/bash
# post-to-discord.sh — CLI to post a message to Discord via selena-project's
# discord_client (no OpenClaw, no LLM, no api_server).
#
# Use this when:
#   - selena-api is down but you still need to post a message
#   - You want to test the discord_client from the shell
#   - The watchdog is down and you want to manually announce something
#   - A cron wants to bypass OpenClaw's broken announce mode and use the
#     in-process bot instead (recommended for all cron jobs after 2026-06-03)
#
# Usage:
#   ./post-to-discord.sh "Hello from the shell!"
#   echo "Reading from stdin" | ./post-to-discord.sh
#   ./post-to-discord.sh -c 1495170712397152367 "Different channel"
#   ./post-to-discord.sh --project selena-project --agent slow-heartbeat --task hourly "..."
#   ./post-to-discord.sh --status
#   ./post-to-discord.sh --stats       # aggregated log stats
#   ./post-to-discord.sh --list 20     # last 20 send log entries
#
# Token resolution: env DISCORD_BOT_TOKEN first, then ~/.openclaw/openclaw.json.
# Channel defaults to DISCORD_DEFAULT_CHANNEL_ID from .env, or #selena-project.
# The send is logged to data/discord_send_log.jsonl with project / agent / task
# tags so the web UI / --stats can break it down by caller.

set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SELENA_DIR="$SCRIPT_DIR/.."
exec python3 "$SELENA_DIR/code/discord_client.py" "$@"
