#!/usr/bin/env python3
"""Logseq MCP server — exposes read_recent_notes() tool."""

import datetime
import logging
import re
import sys
from pathlib import Path

import yaml
from mcp.server.fastmcp import FastMCP

CONFIG_PATH = Path("/home/cam/local_agents/nanobot-brief/config.yaml")


def _setup_logger(name: str) -> logging.Logger:
    import os
    log_file = Path(
        os.environ.get("BRIEFING_LOG_FILE", "/home/cam/daily-briefings/mcp-debug.log")
    )
    log_file.parent.mkdir(parents=True, exist_ok=True)
    log = logging.getLogger(name)
    log.setLevel(logging.DEBUG)
    if not log.handlers:
        fmt = logging.Formatter("%(asctime)s [%(name)s] %(levelname)s: %(message)s")
        fh = logging.FileHandler(log_file)
        fh.setFormatter(fmt)
        log.addHandler(fh)
        # No stderr handler — avoids duplicate lines in the cron/briefing log
    return log


log = _setup_logger("logseq")


def _load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


def _journal_date(path: Path) -> datetime.date | None:
    """Parse YYYY_MM_DD from Logseq journal filename."""
    m = re.match(r"(\d{4})_(\d{2})_(\d{2})\.md$", path.name)
    if m:
        return datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    return None


log.info("logseq MCP server started")
mcp = FastMCP("logseq")


@mcp.tool()
def read_recent_notes() -> str:
    """
    Read recent Logseq notes for the daily briefing.
    Journals are filtered by filename date (last N days) — immune to git clone
    mtime reset. Pages are filtered by filesystem mtime. Output is capped at
    max_notes_chars to fit within the local LLM context window.
    """
    log.info("read_recent_notes called")
    config = _load_config()
    logseq_dir = Path(config.get("logseq_dir", "/home/cam/logseq-graph"))
    journal_days = int(config.get("journal_lookback_days", 3))
    lookback_hours = int(config.get("notes_lookback_hours", 48))
    max_chars = int(config.get("max_notes_chars", 25000))
    log.info("logseq_dir=%s journal_days=%d lookback_hours=%d max_chars=%d",
             logseq_dir, journal_days, lookback_hours, max_chars)

    today = datetime.date.today()
    cutoff_date = today - datetime.timedelta(days=journal_days)
    cutoff_mtime = datetime.datetime.now().timestamp() - (lookback_hours * 3600)

    sections: list[str] = []

    # ── Journals (filename-date based) ──
    journals_dir = logseq_dir / "journals"
    journal_files: list[tuple[datetime.date, Path]] = []
    if journals_dir.exists():
        for md_file in journals_dir.glob("*.md"):
            d = _journal_date(md_file)
            if d is not None and d >= cutoff_date:
                journal_files.append((d, md_file))
    journal_files.sort(key=lambda x: x[0], reverse=True)
    log.info("found %d journal file(s) in last %d days", len(journal_files), journal_days)

    if journal_files:
        sections.append(f"=== Journal entries (last {journal_days} days) ===")
        for d, path in journal_files:
            sections.append(f"\n## {d.strftime('%A, %B %-d %Y')} ({path.name})\n")
            try:
                sections.append(path.read_text(encoding="utf-8"))
            except Exception as e:
                log.error("could not read %s: %s", path.name, e)
                sections.append(f"[Could not read: {e}]")

    # ── Pages (mtime based) ──
    pages_dir = logseq_dir / "pages"
    page_files: list[tuple[float, Path]] = []
    if pages_dir.exists():
        for md_file in pages_dir.glob("*.md"):
            mtime = md_file.stat().st_mtime
            if mtime >= cutoff_mtime:
                page_files.append((mtime, md_file))
    page_files.sort(key=lambda x: x[0], reverse=True)
    log.info("found %d page file(s) modified in last %dh", len(page_files), lookback_hours)

    if page_files:
        sections.append(f"\n=== Recently modified pages (last {lookback_hours}h) ===")
        for _, path in page_files:
            sections.append(f"\n## {path.name}\n")
            try:
                sections.append(path.read_text(encoding="utf-8"))
            except Exception as e:
                log.error("could not read %s: %s", path.name, e)
                sections.append(f"[Could not read: {e}]")

    if not sections:
        log.info("no notes found in configured windows")
        return f"No notes found in the last {journal_days} days / {lookback_hours} hours."

    full_text = "\n".join(sections)

    if len(full_text) > max_chars:
        full_text = full_text[:max_chars]
        full_text += f"\n\n[... truncated at {max_chars} chars to fit LLM context ...]"
        log.info("output truncated to %d chars", max_chars)
    else:
        log.info("returning %d chars of notes", len(full_text))

    return full_text


if __name__ == "__main__":
    mcp.run()
