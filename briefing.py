#!/usr/bin/env python3
"""
Daily briefing generator — standalone script.

Fetches calendar events and Logseq notes directly (no MCP protocol),
summarises with a local Ollama model, and posts to ntfy.sh.

Usage:
  python3 briefing.py [--dry-run]

With --dry-run, prints the briefing to stdout without sending.
"""

import json
import os
import sys
import urllib.request
import urllib.error
from pathlib import Path

_BASE = Path(__file__).parent
_CONFIG = _BASE / "config.yaml"

# MCP server module paths
sys.path.insert(0, str(_BASE / "mcp-servers" / "calendar"))
sys.path.insert(0, str(_BASE / "mcp-servers" / "logseq"))
sys.path.insert(0, str(_BASE / "mcp-servers" / "email"))


def load_config():
    import yaml
    with open(_CONFIG) as f:
        return yaml.safe_load(f)


def fetch_calendar():
    from calendar_mcp import get_todays_schedule
    raw = get_todays_schedule()
    # Strip the "Schedule for ..." header line produced by calendar_mcp so the LLM
    # doesn't echo it; keep only the event lines, stripping leading whitespace.
    lines = raw.splitlines()
    event_lines = [ln.strip() for ln in lines if ln.strip() and not ln.startswith("Schedule for")]
    return "\n".join(event_lines)


def fetch_notes(max_chars: int = 3000):
    from logseq_mcp import read_recent_notes
    notes = read_recent_notes()
    if len(notes) > max_chars:
        notes = notes[:max_chars] + "\n...(truncated)"
    return notes


def fetch_emails() -> str:
    """
    Fetch recent emails via email_mcp.
    Returns the formatted index+bodies string only when real emails were found.
    Returns empty string on error, no accounts configured, or no recent mail
    (so the LLM never sees a 'no emails' placeholder that confuses it).
    """
    try:
        from email_mcp import get_recent_emails
        result = get_recent_emails()
        # Only pass structured content to the LLM; descriptive 'no X' messages
        # are informative for humans/agents but pollute the summarisation prompt.
        if "=== Email Index" not in result:
            print(f"  ({result.strip()})", flush=True)
            return ""
        return result
    except Exception as e:
        print(f"  Warning: email fetch failed: {e}", flush=True)
        return ""


def summarise(calendar: str, notes: str, emails: str, config: dict) -> str:
    """Call Ollama to produce a concise plain-text briefing."""
    model = config.get("llm_model", "hermes3")
    ollama_base = config.get("ollama_base", "http://127.0.0.1:11434")
    url = f"{ollama_base}/v1/chat/completions"

    system_msg = (
        "You produce terse plain-text daily briefings for Cam. "
        "STRICT output rules — any violation is wrong:\n"
        "- Begin directly with the first calendar event (time first)\n"
        "- No section headings, no labels, no headers of any kind\n"
        "- No markdown, no bullet symbols, no preamble, no sign-off\n"
        "- Under 200 words total\n"
        "Output = up to three blocks separated by one blank line each:\n"
        "  block 1: one event per line, format:  HH:MM–HH:MM  Description\n"
        "  block 2: 2-3 sentences of relevant context from the notes (omit if nothing relevant)\n"
        "  block 3: 1-2 sentences on any messages needing attention (omit if none or unimportant)"
    )

    # No English labels on the data sections — the model echoes them.
    # A --- separator is neutral enough not to trigger echo behaviour.
    data_parts = [calendar, "---", notes]
    if emails:
        data_parts += ["---", emails]
    data_parts.append("Write the briefing.")
    user_msg = "\n\n".join(data_parts)

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_msg},
            {"role": "user", "content": user_msg},
        ],
        "stream": False,
        "temperature": 0.3,
        "max_tokens": 512,
    }

    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer sk-local",
        },
    )

    timeout = config.get("llm_timeout_s", 1200)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read())

    content = data["choices"][0]["message"]["content"]
    return _clean_briefing(content.strip())


# Words/phrases hermes3 tends to echo back from the prompt.
# Single-word section labels and multi-word instruction tails are both covered.
_ECHO_LINES = {
    # section labels
    "calendar", "notes", "events", "schedule", "context",
    "today's events", "today's schedule", "recent notes", "today's calendar",
    "email", "emails", "inbox", "messages",
    # instruction tails the model sometimes echoes verbatim
    "write the briefing",
    "write briefing",
    "write the daily briefing",
}

def _clean_briefing(text: str) -> str:
    """Strip bare echo lines that the model repeats from the prompt."""
    cleaned = []
    for line in text.splitlines():
        # Normalise: strip surrounding whitespace, trailing punctuation, lowercase
        normalised = line.strip().rstrip(":.!").lower()
        if normalised in _ECHO_LINES:
            continue  # drop echoed label or instruction
        cleaned.append(line)
    # Drop leading blank lines
    while cleaned and not cleaned[0].strip():
        cleaned.pop(0)
    return "\n".join(cleaned).strip()


def send_notification(text: str, config: dict) -> None:
    """POST the briefing to ntfy.sh."""
    import urllib.request
    topic = config["ntfy_topic"]
    url = f"https://ntfy.sh/{topic}"

    req = urllib.request.Request(
        url,
        data=text.encode("utf-8"),
        headers={
            "Title": "Daily Briefing",
            "Priority": "default",
            "Tags": "spiral_calendar",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        status = resp.status
    print(f"ntfy.sh POST → HTTP {status}")


def main():
    dry_run = "--dry-run" in sys.argv

    config = load_config()
    print("Fetching calendar...", flush=True)
    calendar = fetch_calendar()
    print(f"  {len(calendar.splitlines())} lines")

    print("Fetching notes...", flush=True)
    notes = fetch_notes(max_chars=3000)
    print(f"  {len(notes)} chars")

    print("Fetching emails...", flush=True)
    emails = fetch_emails()
    print(f"  {len(emails)} chars")

    print("Summarising with LLM...", flush=True)
    briefing = summarise(calendar, notes, emails, config)
    print(f"  {len(briefing)} chars, {len(briefing.split())} words")

    print()
    print("=== BRIEFING ===")
    print(briefing)
    print("================")

    if dry_run:
        print("\n[dry-run — not sending]")
        return

    print("\nSending to ntfy.sh...", flush=True)
    send_notification(briefing, config)
    print("Done.")


if __name__ == "__main__":
    main()
