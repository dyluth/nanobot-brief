#!/usr/bin/env python3
"""
Daily briefing generator — standalone script.

Fetches calendar events and Logseq notes directly (no MCP protocol),
summarises with a local Ollama model, and posts to ntfy.sh.

Usage:
  python3 briefing.py [--dry-run]

With --dry-run, prints the briefing to stdout without sending.
"""

import datetime
import json
import os
import re
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
    notes = _strip_logseq_markdown(read_recent_notes())
    if len(notes) > max_chars:
        notes = notes[:max_chars] + "\n...(truncated)"
    return notes


def _strip_logseq_markdown(text: str) -> str:
    """
    Remove Logseq-specific markdown syntax so the LLM receives clean plain text
    and doesn't parrot formatting artefacts into the briefing output.

    Drops entirely:
    - DONE task lines (both **[DONE]** index format and "- DONE" journal format)
    - === section headers produced by logseq_mcp (=== Journal entries ===, etc.)
    - Journal date-header lines containing YYYY_MM_DD.md filename references
    - Bare bullet points with no content
    - Structural metadata lines (Generated:, Activity:, etc.)
    - Activity-count summary lines ("- 1 DONE task", etc.)

    Strips markers from (keeps text):
    - Markdown headings (#/##/###)
    - Active task status prefixes (**[TODO]** / **[NOW]** / **[DOING]** / **[LATER]**)
    - **bold** markers, [[wiki links]], `backtick refs`, ⏱ time markers, #A/#B/#C tags
    """
    # Lines to drop (startswith checks applied after inline stripping)
    _SKIP = (
        "Generated:", "Last ", "Journal:", "Activity:", "Tasks (",
        "Time Logged:", "Time Tracking:",
        "Recent Activity Timeline", "Knowledge Dashboard",
        "---", "*Pages with",
        "📊", "📅", "🔗", "📝",
    )
    _ACTIVITY_COUNT = re.compile(
        r"^-?\s*\d+\s+(NOW|TODO|DONE|DOING|LATER)\s+tasks?\b", re.IGNORECASE
    )
    lines = []
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue

        # ── Drop completed task lines before any stripping ────────────────────
        # Index file format:   - **[DONE]** task text
        if re.search(r"\*\*\[DONE\]\*\*", s):
            continue
        # Journal file format: - DONE task text
        if re.match(r"^\s*-\s+DONE\b", s, re.IGNORECASE):
            continue

        # ── Drop === section headers (logseq_mcp output structure) ───────────
        if re.match(r"^===.*===$", s):
            continue

        # ── Drop journal date-header lines  e.g. "Thursday, May 28 (2026_05_28.md)" ──
        if re.search(r"\d{4}_\d{2}_\d{2}\.md", s):
            continue

        # ── Strip inline markdown ─────────────────────────────────────────────
        s = re.sub(r"^#{1,3}\s+", "", s)
        # Active task status prefixes only (DONE already dropped above)
        s = re.sub(r"\*\*\[(TODO|NOW|DOING|LATER)\]\*\*\s*[-–]?\s*", "", s)
        s = re.sub(r"\*\*(.+?)\*\*", r"\1", s)
        s = re.sub(r"\[\[([^\]]+)\]\]", r"\1", s)
        s = re.sub(r"`[^`]+`", "", s)
        s = re.sub(r"⏱\s*[\dhms ]+", "", s)
        s = re.sub(r"(?<!\w)#[ABC](?!\w)\s*", "", s)
        s = " ".join(s.split())

        # ── Drop lines that are now empty or bare bullets ─────────────────────
        if not s or s in ("-", "–", "—"):
            continue
        if any(s.startswith(p) for p in _SKIP):
            continue
        if _ACTIVITY_COUNT.match(s):
            continue
        lines.append(s)
    return "\n".join(lines)


# Matches [[Jun 3rd, 2026]], [[Jun 3, 2026]], [[November 24th, 2025]], etc.
_FUTURE_DATE_RE = re.compile(
    r"\[\[(?P<mon>Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?"
    r"|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
    r"\.?\s+(?P<day>\d{1,2})(?:st|nd|rd|th)?,?\s+(?P<year>\d{4})\]\]",
    re.IGNORECASE,
)
_NEXT_WEEK_RE = re.compile(
    r"\bnext\s+(?:week|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
    re.IGNORECASE,
)
_MONTH_MAP = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def _is_future_task(task_line: str, today: datetime.date) -> bool:
    """Return True if the task is explicitly dated more than 2 days from today."""
    if _NEXT_WEEK_RE.search(task_line):
        return True
    for m in _FUTURE_DATE_RE.finditer(task_line):
        try:
            month = _MONTH_MAP[m.group("mon")[:3].lower()]
            task_date = datetime.date(int(m.group("year")), month, int(m.group("day")))
            if (task_date - today).days > 2:
                return True
        except (ValueError, KeyError):
            pass
    return False


def _extract_top_projects(dashboard_text: str) -> str:
    """Extract only projects with active tasks from the Top Projects section."""
    lines = dashboard_text.splitlines()
    in_projects = False
    result = ["Top active projects:"]
    for line in lines:
        if "Top Projects" in line:
            in_projects = True
            continue
        if in_projects:
            if line.startswith("## "):
                break  # next section — stop
            s = line.strip()
            # Only include projects that have at least one active task
            if s and not s.startswith("*") and "active task" in s.lower():
                result.append(_strip_logseq_markdown(s))
    return "\n".join(result) if len(result) > 1 else ""


def _extract_active_tasks(status_text: str) -> str:
    """
    Extract the most actionable tasks from tasks-by-status.md content.
    Keeps: all DOING tasks; NOW/TODO tasks from 2026 journals; #A/#B
    priority tasks from project pages.
    Drops: Logseq query artefacts ('LATER DOING)'), stale 2025 entries,
    and tasks explicitly dated more than 2 days in the future.
    """
    today = datetime.date.today()
    lines = status_text.splitlines()
    doing, active = [], []
    current = None

    for line in lines:
        if line.startswith("## DOING"):
            current = "DOING"
        elif line.startswith("## NOW") or line.startswith("## TODO"):
            current = "ACTIVE"
        elif line.startswith("## "):
            current = None

        if not line.startswith("- **"):
            continue

        is_artifact = "LATER DOING)" in line or "DOING)" in line
        if is_artifact:
            continue

        if _is_future_task(line, today):
            continue

        if current == "DOING":
            doing.append(line)
        elif current == "ACTIVE":
            is_recent_journal = "`journals/2026_" in line
            is_priority_page  = ("`pages/" in line) and ("#A " in line or "#B " in line)
            if is_recent_journal or is_priority_page:
                active.append(line)

    parts = []
    if doing:
        parts.append("In Progress: " + "; ".join(doing))
    if active:
        parts.append("Active tasks: " + "; ".join(active))
    return "\n".join(parts)


def fetch_tasks(config: dict, max_chars: int = 1500) -> str:
    """
    Read task indexes from logseq_dir/.claude/indexes/.
    Combines:
    - timeline-recent.md  (last 7 days of journal activity)
    - dashboard.md        (top active projects)
    - tasks-by-status.md  (filtered to recent/priority active tasks)
    All Logseq markdown is stripped before the text is returned, so the LLM
    sees clean plain text rather than raw **bold** / [[wiki]] syntax.
    Returns empty string if the indexes directory doesn't exist.
    """
    logseq_dir = Path(config.get("logseq_dir", Path.home() / "logseq-graph"))
    indexes_dir = logseq_dir / ".claude" / "indexes"
    if not indexes_dir.exists():
        return ""

    parts: list[str] = []

    # Recent journal activity (last 7 days) — filter future-dated lines before strip removes [[date]] markers
    timeline = indexes_dir / "timeline-recent.md"
    if timeline.exists():
        raw = timeline.read_text(encoding="utf-8")
        today = datetime.date.today()
        filtered = "\n".join(
            line for line in raw.splitlines()
            if not _is_future_task(line, today)
        )
        parts.append(_strip_logseq_markdown(filtered))

    # Top active projects from dashboard (concise — just the projects section)
    dashboard = indexes_dir / "dashboard.md"
    if dashboard.exists():
        proj = _extract_top_projects(dashboard.read_text(encoding="utf-8"))
        if proj:
            parts.append(proj)

    # Active tasks: DOING + recent NOW/TODO
    status_file = indexes_dir / "tasks-by-status.md"
    if status_file.exists():
        extracted = _extract_active_tasks(status_file.read_text(encoding="utf-8"))
        if extracted:
            parts.append(_strip_logseq_markdown(extracted))

    if not parts:
        return ""

    result = "\n\n".join(p for p in parts if p.strip())
    if len(result) > max_chars:
        result = result[:max_chars] + "\n... [task list truncated]"
    return result


def _header_check_llm_call(meta: dict, config: dict) -> bool:
    """
    Ask the LLM whether a single email header is worth reading in full.
    Returns True if the email looks like it needs Cam's personal attention.
    Returns False on error (safe default — skip rather than waste a body fetch).
    """
    model = config.get("llm_model", "hermes3")
    ollama_base = config.get("ollama_base", "http://127.0.0.1:11434")
    url = f"{ollama_base}/v1/chat/completions"

    system_msg = (
        "Reply with exactly one word: yes or no. "
        "Reply 'yes' if this email: (a) is from a real person needing a reply or decision; "
        "(b) is a security or account alert (e.g. from Google, Apple, a bank, or payment provider); "
        "or (c) is a practical local reminder (e.g. bin/refuse collection, appointment, delivery). "
        "Reply 'no' for: newsletters, marketing, promotions, mailing lists, social media "
        "notifications, charity solicitations, and automated system emails that don't require action."
    )
    user_msg = f"From: {meta['from_addr']}\nSubject: {meta['subject']}"

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_msg},
            {"role": "user", "content": user_msg},
        ],
        "stream": False,
        "temperature": 0.0,
        "max_tokens": 5,
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
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
        response = data["choices"][0]["message"]["content"].strip().lower()
        return "yes" in response
    except Exception as e:
        print(f"  Warning: header check LLM call failed: {e}", flush=True)
        return False


def _evaluate_email_llm_call(meta: dict, body: str, config: dict) -> str:
    """
    Ask the LLM to summarise what action/info is needed from a single email.
    Returns a one-sentence summary, or 'nothing' if the email is not actionable.
    Returns 'nothing' on error (caller treats it as discard).
    """
    model = config.get("llm_model", "hermes3")
    ollama_base = config.get("ollama_base", "http://127.0.0.1:11434")
    url = f"{ollama_base}/v1/chat/completions"

    system_msg = (
        "Summarise this email in ONE sentence describing what personal action Cam needs "
        "to take or what genuinely important information it contains. "
        "Only keep the email if a real person is expecting a reply from Cam, or Cam has "
        "a specific task or deadline arising from it. "
        "If this is a promotional, marketing, automated, or mailing-list email — "
        "or if no personal response or action is required — reply exactly: nothing"
    )
    user_msg = f"From: {meta['from_addr']}\nSubject: {meta['subject']}\n\n{body}"
    # Truncate very long bodies — the evaluator doesn't need more than ~4 KB
    if len(user_msg) > 4000:
        user_msg = user_msg[:4000] + "\n... [truncated]"

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_msg},
            {"role": "user", "content": user_msg},
        ],
        "stream": False,
        "temperature": 0.2,
        "max_tokens": 100,
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
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
        return data["choices"][0]["message"]["content"].strip()
    except Exception as e:
        print(f"  Warning: evaluator LLM call failed for '{meta['subject'][:40]}': {e}", flush=True)
        return "nothing"


_EMAIL_MAX_TO_CHECK = 100  # examine at most this many headers per run
_EMAIL_MAX_TO_KEEP  = 5   # stop after collecting this many actionable summaries

# Keyword bypass: these pass straight through without calling the LLM header check.
# Security alerts: known sender domains paired with security-related subject keywords.
# Practical reminders: subject-line is itself the actionable info; no body needed.
_SECURITY_SENDER_RE = re.compile(
    r"accounts\.google\.com|accounts\.apple\.com|account\.live\.com|account\.microsoft\.com",
    re.IGNORECASE,
)
_SECURITY_SUBJECT_RE = re.compile(
    r"security\s+alert|sign.?in\s+(attempt|detected)|account\s+(access|breach|compromis)",
    re.IGNORECASE,
)
_REMINDER_SUBJECT_RE = re.compile(
    r"\b(refuse\s+collection|bin\s+collection|collection\s+remind)\b",
    re.IGNORECASE,
)
# Personal-name sender: single initial + dot + surname (e.g. a.bacci@, k.mcbride@).
# The LLM associates financial/corporate domains with marketing and wrongly skips these.
# Matching on address structure is more reliable than asking the model.
_PERSONAL_INITIAL_RE = re.compile(r"^[a-z]\.[a-z]{2,20}@", re.IGNORECASE)


def fetch_emails(config: dict) -> str:
    """
    Sequential per-email LLM processing:
      1. Fetch all email headers (IMAP index phase)
      2. For each email in recency order (up to _EMAIL_MAX_TO_CHECK):
           a. Header check LLM call: is this from a real person needing attention? (yes/no)
           b. If yes: fetch that email's body individually
           c. Evaluator LLM call: what action is required? (one sentence or 'nothing')
           d. If actionable: add to summaries; stop once _EMAIL_MAX_TO_KEEP reached

    Each email gets its own focused LLM calls rather than overwhelming a small
    local model with a large batch. Each body fetch is independently capped at
    per_email_limit chars (set in email_mcp.get_email_bodies_for).

    Returns a bulleted list of actionable summaries, or empty string if none found.
    """
    try:
        from email_mcp import get_email_index, get_email_bodies_for

        _, email_list = get_email_index()
        if not email_list:
            print("  (no emails in last 24h)", flush=True)
            return ""

        total = len(email_list)
        check_up_to = min(total, _EMAIL_MAX_TO_CHECK)
        print(f"  {total} emails indexed; checking top {check_up_to} sequentially...", flush=True)

        summaries: list[str] = []
        for i, meta in enumerate(email_list[:check_up_to], 1):
            if len(summaries) >= _EMAIL_MAX_TO_KEEP:
                break

            sender = meta.get("from_addr", "")
            subj_full = meta.get("subject", "")
            print(f"  [{i}/{check_up_to}] {sender[:30]} | {subj_full[:45]}", flush=True)

            # ── Header check (keyword bypass or LLM) ─────────────────────────
            is_security = bool(_SECURITY_SENDER_RE.search(sender)) and bool(
                _SECURITY_SUBJECT_RE.search(subj_full)
            )
            is_reminder = bool(_REMINDER_SUBJECT_RE.search(subj_full))
            is_personal = bool(_PERSONAL_INITIAL_RE.match(sender))

            if is_reminder:
                # Subject IS the actionable information — no body fetch needed
                summaries.append(f"Reminder: {subj_full}")
                print(f"    → kept (reminder): {subj_full[:80]}", flush=True)
                continue

            if not is_security and not is_personal and not _header_check_llm_call(meta, config):
                print(f"    → skip", flush=True)
                continue

            # ── Fetch this email's body individually ──────────────────────────
            results = get_email_bodies_for(email_list, [i])
            if not results or not results[0][1]:
                if is_security:
                    # Body is HTML-only or unavailable; subject is still worth surfacing
                    summaries.append(f"Security alert: {subj_full}")
                    print(f"    → kept (security, no body): {subj_full[:80]}", flush=True)
                else:
                    print(f"    → no body", flush=True)
                continue
            _, body = results[0]

            # ── Evaluate the body ─────────────────────────────────────────────
            print(f"    → evaluating body...", flush=True)
            result = _evaluate_email_llm_call(meta, body, config)
            cleaned = result.strip()
            if cleaned.lower() not in ("nothing", "none", ""):
                summaries.append(cleaned)
                print(f"    → kept: {cleaned[:80]}", flush=True)
            else:
                print(f"    → discarded after body read", flush=True)

        if not summaries:
            print("  (no actionable emails)", flush=True)
            return ""

        print(f"  {len(summaries)} actionable email(s) for briefing", flush=True)
        return "\n".join(f"- {s}" for s in summaries)

    except Exception as e:
        print(f"  Warning: email fetch failed: {e}", flush=True)
        return ""


def summarise(calendar: str, tasks: str, notes: str, config: dict) -> str:
    """Call Ollama to produce a concise plain-text briefing. Emails are appended by caller."""
    model = config.get("llm_model", "hermes3")
    ollama_base = config.get("ollama_base", "http://127.0.0.1:11434")
    url = f"{ollama_base}/v1/chat/completions"

    system_msg = (
        "You produce terse plain-text daily briefings for Cam. "
        "STRICT output rules — any violation is wrong:\n"
        "- Begin directly with the first calendar event (time first)\n"
        "- No section headings, no labels, no headers of any kind\n"
        "- No markdown, no bullet symbols, no preamble, no sign-off\n"
        "- Under 250 words total\n"
        "Output = up to three blocks separated by one blank line each:\n"
        "  block 1: calendar — one event per line, format:  HH:MM–HH:MM  Description\n"
        "  block 2: tasks — 3-6 plain lines, each one actionable task from the task index "
                          "(most recent / highest priority first; omit block if no tasks)\n"
        "  block 3: context — at most 1 sentence of factual context directly relevant to today; "
                          "state only what is explicitly written, do not infer relationships or "
                          "intentions; omit entirely if nothing is clearly relevant to today"
    )

    # No English labels on the data sections — the model echoes them.
    # A --- separator is neutral enough not to trigger echo behaviour.
    data_parts = [calendar, "---"]
    if tasks:
        data_parts += [tasks, "---"]
    data_parts.append(notes)
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
    "tasks", "active tasks", "in progress", "task index",
    "comms", "communications",
    # instruction tails the model sometimes echoes verbatim
    "write the briefing",
    "write briefing",
    "write the daily briefing",
}

# Regex patterns for "nothing relevant here" sentences the model produces
# instead of simply omitting the block as instructed.
_NOTHING_HERE = re.compile(
    r"^no (email|emails?|message|messages?|comm|comms?|task|tasks?)"
    r"(\s+\w+){0,5}[.!]?$",
    re.IGNORECASE,
)

def _clean_briefing(text: str) -> str:
    """Strip bare echo lines and 'nothing here' sentences the model produces."""
    cleaned = []
    for line in text.splitlines():
        stripped = line.strip()
        # Drop horizontal rules the model copies from prompt separators
        if stripped == "---":
            continue
        # Normalise: strip trailing punctuation, lowercase for set lookup
        normalised = stripped.rstrip(":.!").lower()
        if normalised in _ECHO_LINES:
            continue
        # Drop "No emails need a reply." style filler sentences
        if _NOTHING_HERE.match(stripped):
            continue
        cleaned.append(line)
    # Drop leading blank lines
    while cleaned and not cleaned[0].strip():
        cleaned.pop(0)
    text = "\n".join(cleaned).strip()
    # Collapse 3+ consecutive blank lines to one (LLM sometimes over-separates blocks)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text


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
            "Priority": "high",
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

    print("Fetching tasks...", flush=True)
    tasks = fetch_tasks(config)
    print(f"  {len(tasks)} chars")

    print("Fetching notes...", flush=True)
    notes = fetch_notes(max_chars=3000)
    print(f"  {len(notes)} chars")

    print("Fetching emails...", flush=True)
    emails = fetch_emails(config)
    print(f"  {len(emails)} chars")

    print("Summarising with LLM...", flush=True)
    briefing = summarise(calendar, tasks, notes, config)
    if emails:
        briefing = briefing.rstrip() + "\n\n" + emails
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
