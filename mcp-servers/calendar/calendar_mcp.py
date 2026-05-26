#!/usr/bin/env python3
"""Calendar MCP server — exposes get_todays_schedule() tool."""

import datetime
import os
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import recurring_ical_events
import requests
import yaml
from icalendar import Calendar
from mcp.server.fastmcp import FastMCP

CONFIG_PATH = Path("/home/cam/nanobot-brief/config.yaml")

def _load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)

def _local_tz() -> ZoneInfo:
    try:
        import tzlocal
        return ZoneInfo(str(tzlocal.get_localzone()))
    except Exception:
        return ZoneInfo("UTC")

def _fetch_events_for_today(ics_url: str, today: datetime.date, tz: ZoneInfo) -> list[dict]:
    """Fetch one ICS feed and return events occurring on today."""
    try:
        resp = requests.get(ics_url, timeout=15)
        resp.raise_for_status()
    except Exception as e:
        return [{"error": f"Failed to fetch {ics_url}: {e}"}]

    cal = Calendar.from_ical(resp.content)
    start = datetime.datetime.combine(today, datetime.time.min, tzinfo=tz)
    end = start + datetime.timedelta(days=1)

    events = []
    for component in recurring_ical_events.of(cal).between(start, end):
        if component.name != "VEVENT":
            continue

        dtstart = component.get("DTSTART").dt
        dtend_prop = component.get("DTEND") or component.get("DURATION")

        # Normalise to datetime
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

    return events


mcp = FastMCP("calendar")


@mcp.tool()
def get_todays_schedule() -> str:
    """
    Fetch today's meetings from all configured calendar feeds.
    Returns a chronological, deduplicated list of events for today.
    """
    config = _load_config()
    feeds = config.get("calendar_feeds", [])
    tz = _local_tz()
    today = datetime.date.today()

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
        return f"No events found for {today.isoformat()}."

    # Deduplicate by (start, title) and sort chronologically
    seen = set()
    unique: list[dict] = []
    for ev in sorted(all_events, key=lambda e: e["start"]):
        key = (ev["start"].isoformat(), ev["title"])
        if key not in seen:
            seen.add(key)
            unique.append(ev)

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
