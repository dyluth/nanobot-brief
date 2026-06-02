#!/bin/bash
# set -e intentionally NOT used — some steps may exit non-zero (e.g. ICS network errors)
# but we still want the script to continue and attempt the send.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LOG_DIR=/home/cam/daily-briefings

# Per-run timestamped log files — one pair per execution, never appended across runs
RUN_TS=$(date '+%Y-%m-%d-%H%M%S')
LOG="${LOG_DIR}/briefing-${RUN_TS}.log"
export BRIEFING_LOG_FILE="${LOG_DIR}/mcp-debug-${RUN_TS}.log"

# Keep logs from the last 14 days; silently ignore errors if dir is empty
find "$LOG_DIR" -name "briefing-*.log"   -mtime +14 -delete 2>/dev/null || true
find "$LOG_DIR" -name "mcp-debug-*.log" -mtime +14 -delete 2>/dev/null || true

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
