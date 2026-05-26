#!/bin/bash
# set -e intentionally NOT used for the nanobot call — nanobot exits non-zero
# on MCP cleanup (CancelledError in close_mcp) even after successful completion.
set -uo pipefail

LOG=/home/cam/daily-briefings/cron.log

echo "" >> "$LOG"
echo "=== $(date '+%Y-%m-%d %H:%M:%S') ===" >> "$LOG"

# Pull latest Logseq notes
echo "[1/3] Syncing Logseq..." >> "$LOG"
cd /home/cam/logseq-graph && git pull origin main >> "$LOG" 2>&1

# Activate venv
source /home/cam/agent-env/bin/activate

# Raise nanobot's HTTP timeouts for local CPU inference.
# Default 120s/90s are too short for llama3.1:8b (~180-300s per response on CPU).
# 600s = 10 min gives headroom for cold model loads and long generations.
# To switch models, update llm_model in config.yaml and "model" in ~/.nanobot/config.json.
export NANOBOT_OPENAI_COMPAT_TIMEOUT_S=600
export NANOBOT_STREAM_IDLE_TIMEOUT_S=300

# Run nanobot agent.
# Prompt keeps instruction concise to reduce tokens-to-process.
# ntfy_mcp.py guards against file-reference strings reaching the notification.
echo "[2/3] Running nanobot agent..." >> "$LOG"
nanobot agent --logs --no-markdown -m "\
Call get_todays_schedule (no args) then read_recent_notes (no args). \
Write a concise daily briefing for Cam: list today's meetings (time and title), \
then 3-5 bullet points of key themes or action items from the notes. \
Under 400 words, no preamble. \
Finally call send_briefing_to_cam with the briefing text as the message — \
pass the actual text, not a file reference." \
  >> "$LOG" 2>&1 || echo "[warn] nanobot exited non-zero (likely MCP cleanup teardown — check above for actual errors)" >> "$LOG"

echo "[3/3] Done." >> "$LOG"
