#!/bin/bash
# set -e intentionally NOT used — some steps may exit non-zero (e.g. ICS network errors)
# but we still want the script to continue and attempt the send.
set -uo pipefail

LOG=/home/cam/daily-briefings/cron.log
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "" >> "$LOG"
echo "=== $(date '+%Y-%m-%d %H:%M:%S') ===" >> "$LOG"

# Pull latest Logseq notes
echo "[1/3] Syncing Logseq..." >> "$LOG"
cd /home/cam/logseq-graph && git pull origin main >> "$LOG" 2>&1

# Activate venv
source /home/cam/agent-env/bin/activate

# Run briefing.py:
#   1. Fetches calendar events directly (via calendar_mcp.py functions)
#   2. Fetches Logseq notes directly (via logseq_mcp.py functions)
#   3. Calls Ollama (stream=False) to summarise the pre-fetched data
#   4. Posts the result to ntfy.sh
#
# This approach avoids multi-step LLM tool-orchestration, which small 8B models
# (hermes3, llama3.1:8b) fail at unreliably in nanobot's complex 12K system-prompt
# context.  The LLM's only job here is a single summarisation call.
echo "[2/3] Running briefing pipeline..." >> "$LOG"
python3 "$SCRIPT_DIR/briefing.py" >> "$LOG" 2>&1

echo "[3/3] Done." >> "$LOG"
