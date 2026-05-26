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

# Run nanobot agent
# Prompt is explicit: no intermediary files, pass text directly to send_briefing_to_cam.
# The || true swallows nanobot's non-zero exit from the MCP cleanup CancelledError,
# which fires after the work is already done.
echo "[2/3] Running nanobot agent..." >> "$LOG"
nanobot agent --logs --no-markdown -m "\
Step 1: Call get_todays_schedule with no arguments to get today's calendar. \
Step 2: Call read_recent_notes with no arguments to get recent Logseq notes. \
Step 3: Using only the results from steps 1 and 2, write a concise daily briefing for Cam. \
  Format: first list today's meetings (time, title), then 3-5 bullet points of themes/action items from recent notes. \
  Keep it under 400 words. No preamble, no sign-off, no meta-commentary. \
Step 4: Call send_briefing_to_cam, passing the complete briefing text as the message argument directly. \
  Do NOT use write_file or read_file. Pass the actual text string — not a file reference." \
  >> "$LOG" 2>&1 || echo "[warn] nanobot exited non-zero (likely MCP cleanup teardown — check above for actual errors)" >> "$LOG"

echo "[3/3] Done." >> "$LOG"
