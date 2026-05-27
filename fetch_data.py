#!/usr/bin/env python3
"""
Directly invoke calendar and logseq MCP server functions without going through MCP.
Imports the server modules and calls their tool functions directly.
Outputs the result to stdout.

Usage:
  python3 fetch_data.py calendar
  python3 fetch_data.py logseq
"""
import sys
import os

# Add MCP server directories to path
_BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_BASE, "mcp-servers", "calendar"))
sys.path.insert(0, os.path.join(_BASE, "mcp-servers", "logseq"))
sys.path.insert(0, os.path.join(_BASE, "mcp-servers", "notify"))


def fetch_calendar():
    from calendar_mcp import get_todays_schedule
    return get_todays_schedule()


def fetch_logseq():
    from logseq_mcp import read_recent_notes
    return read_recent_notes()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: fetch_data.py calendar|logseq", file=sys.stderr)
        sys.exit(1)

    cmd = sys.argv[1].lower()
    if cmd == "calendar":
        print(fetch_calendar())
    elif cmd == "logseq":
        print(fetch_logseq())
    else:
        print(f"Unknown command: {cmd}", file=sys.stderr)
        sys.exit(1)
