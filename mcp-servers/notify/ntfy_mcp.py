#!/usr/bin/env python3
"""Notify MCP server — exposes send_briefing_to_cam(message) tool.

The ntfy.sh destination URL is resolved from config at startup and is never
exposed as a tool argument, preserving the air-gap security constraint.
"""

from pathlib import Path

import requests
import yaml
from mcp.server.fastmcp import FastMCP

CONFIG_PATH = Path("/home/cam/nanobot-brief/config.yaml")

def _build_ntfy_url() -> str:
    with open(CONFIG_PATH) as f:
        cfg = yaml.safe_load(f)
    topic = cfg["ntfy_topic"]
    return f"https://ntfy.sh/{topic}"

# Resolved once at import time — agent tool cannot modify it
_NTFY_URL: str = _build_ntfy_url()

mcp = FastMCP("notify")


@mcp.tool()
def send_briefing_to_cam(message: str) -> str:
    """
    Send the daily briefing to Cam via push notification.
    Accepts exactly one argument: the complete briefing text.
    """
    resp = requests.post(
        _NTFY_URL,
        data=message.encode("utf-8"),
        headers={
            "Title": "Daily Briefing",
            "Priority": "default",
            "Tags": "spiral_calendar",
        },
        timeout=15,
    )
    resp.raise_for_status()
    return f"Briefing sent successfully (HTTP {resp.status_code})."


if __name__ == "__main__":
    mcp.run()
