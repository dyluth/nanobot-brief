#!/usr/bin/env python3
"""Calendar MCP server — exposes get_todays_schedule() tool."""

import datetime
import logging
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import recurring_ical_events
import requests
import yaml
from icalendar import Calendar
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


log = _setup_logger("calendar")


def _load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


def _local_tz() -> ZoneInfo:
    """
    Return the display timezone for calendar events.
    Prefers the 'timezone' key in config.yaml so the briefing shows correct local
    time even when the server's system clock is set to UTC (common on Linux VMs).
    Falls back to tzlocal, then UTC.
    """
    try:
        cfg = _load_config()
        tz_name = cfg.get("timezone", "").strip()
        if tz_name:
            return ZoneInfo(tz_name)
    except Exception:
        pass
    try:
        import tzlocal
        return ZoneInfo(str(tzlocal.get_localzone()))
    except Exception:
        return ZoneInfo("UTC")


def _fetch_events_for_today(ics_url: str, today: datetime.date, tz: ZoneInfo) -> list[dict]:
    """Fetch one ICS feed and return events occurring on today."""
    # Log only the non-secret part of the URL (up to the email address)
    url_label = ics_url.split("/ical/")[1].split("/")[0] if "/ical/" in ics_url else ics_url[:40]
    log.info("fetching ICS feed for: %s", url_label)
    try:
        resp = requests.get(ics_url, timeout=15)
        resp.raise_for_status()
        log.info("ICS fetch OK — %d bytes", len(resp.content))
    except Exception as e:
        log.error("ICS fetch failed for %s: %s", url_label, e)
        return [{"error": f"Failed to fetch calendar ({url_label}): {e}"}]

    cal = Calendar.from_ical(resp.content)
    start = datetime.datetime.combine(today, datetime.time.min, tzinfo=tz)
    end = start + datetime.timedelta(days=1)

    events = []
    for component in recurring_ical_events.of(cal).between(start, end):
        if component.name != "VEVENT":
            continue

        dtstart = component.get("DTSTART").dt
        dtend_prop = component.get("DTEND") or component.get("DURATION")

        if isinstance(dtstart, datetime.date) and not isinstance(dtstart, datetime.datetime):
            dtstart = datetime.datetime.combine(dtstart, datetime.time.min, tzinfo=tz)
        elif dtstart.tzinfo is None:
            dtstart = dtstart.replace(tzinfo=tz)
        else:
            dtstart = dtstart.astimezone(tz)

        if dtend_prop is not None and hasattr(dtend_prop, "dt"):
            dtend = dtend_prop.dt
            if isinstance(dtend, datetime.date) and not isinstance(dtend, datetime.datetime):
                dtend = datetime.datetime.combine(dtend, datetime.time.min, tzinfo=tz)
            elif isinstance(dtend, datetime.datetime):
                if dtend.tzinfo is None:
                    dtend = dtend.replace(tzinfo=tz)
                else:
                    dtend = dtend.astimezone(tz)
        else:
            dtend = dtstart + datetime.timedelta(hours=1)

        events.append({
            "start": dtstart,
            "end": dtend,
            "title": str(component.get("SUMMARY", "Untitled")),
            "location": str(component.get("LOCATION", "")),
        })

    log.info("found %d events for today in this feed", len(events))
    return events


log.info("calendar MCP server started")
mcp = FastMCP("calendar")


@mcp.tool()
def get_todays_schedule() -> str:
    """
    Fetch today's meetings from all configured calendar feeds.
    Returns a chronological, deduplicated list of events for today.
    """
    log.info("get_todays_schedule called")
    config = _load_config()
    feeds = config.get("calendar_feeds", [])
    log.info("processing %d calendar feed(s)", len(feeds))
    tz = _local_tz()
    today = datetime.date.today()
    log.info("fetching events for %s (tz: %s)", today.isoformat(), tz)

    all_events: list[dict] = []
    errors: list[str] = []

    for url in feeds:
        results = _fetch_events_for_today(url, today, tz)
        for item in results:
            if "error" in item:
                errors.append(item["error"])
            else:
                all_events.append(item)

    if not all_events and not errors:
        log.info("no events found for today")
        return f"No events found for {today.isoformat()}."

    seen = set()
    unique: list[dict] = []
    for ev in sorted(all_events, key=lambda e: e["start"]):
        key = (ev["start"].isoformat(), ev["title"])
        if key not in seen:
            seen.add(key)
            unique.append(ev)

    log.info("returning %d unique events", len(unique))
    lines = [f"Schedule for {today.strftime('%A, %B %-d %Y')}:"]
    for ev in unique:
        start_str = ev["start"].strftime("%H:%M")
        end_str = ev["end"].strftime("%H:%M")
        loc = f"  [{ev['location']}]" if ev["location"] else ""
        lines.append(f"  {start_str}–{end_str}  {ev['title']}{loc}")

    if errors:
        lines.append("\nWarnings:")
        for err in errors:
            lines.append(f"  ! {err}")

    return "\n".join(lines)


if __name__ == "__main__":
    mcp.run()
