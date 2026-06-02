#!/usr/bin/env python3
"""Notify MCP server — exposes send_briefing_to_cam(message) tool.

The ntfy.sh destination URL is resolved from config at startup and is never
exposed as a tool argument, preserving the air-gap security constraint.
"""

import logging
import sys
from pathlib import Path

import requests
import yaml
from mcp.server.fastmcp import FastMCP

CONFIG_PATH = Path("/home/cam/local_agents/nanobot-brief/config.yaml")
LOG_FILE = Path("/home/cam/daily-briefings/mcp-debug.log")


def _setup_logger(name: str) -> logging.Logger:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    log = logging.getLogger(name)
    log.setLevel(logging.DEBUG)
    if not log.handlers:
        fmt = logging.Formatter("%(asctime)s [%(name)s] %(levelname)s: %(message)s")
        fh = logging.FileHandler(LOG_FILE)
        fh.setFormatter(fmt)
        log.addHandler(fh)
        sh = logging.StreamHandler(sys.stderr)
        sh.setFormatter(fmt)
        log.addHandler(sh)
    return log


log = _setup_logger("notify")


def _build_ntfy_url() -> str:
    with open(CONFIG_PATH) as f:
        cfg = yaml.safe_load(f)
    topic = cfg["ntfy_topic"]
    return f"https://ntfy.sh/{topic}"


# Resolved once at import time — agent tool cannot modify it
_NTFY_URL: str = _build_ntfy_url()
log.info("notify MCP server started — destination topic: %s", _NTFY_URL.split("/")[-1])

mcp = FastMCP("notify")


@mcp.tool()
def send_briefing_to_cam(message: str) -> str:
    """
    Send the daily briefing to Cam via push notification.
    Accepts exactly one argument: the complete briefing text.
    """
    log.info("send_briefing_to_cam called — message length: %d chars", len(message))
    log.info("message preview (first 300 chars): %s", message[:300].replace("\n", "\\n"))

    if not message or message.strip().startswith("["):
        log.warning("message looks like a file reference or is empty — aborting send")
        return "ERROR: message appears to be a file reference, not actual text. Pass the briefing text directly."

    try:
        resp = requests.post(
            _NTFY_URL,
            data=message.encode("utf-8"),
            headers={
                "Title": "Daily Briefing",
                "Priority": "high",
                "Tags": "spiral_calendar",
            },
            timeout=15,
        )
        resp.raise_for_status()
        log.info("ntfy.sh POST succeeded — HTTP %d", resp.status_code)
        return f"Briefing sent successfully (HTTP {resp.status_code})."
    except requests.HTTPError as e:
        log.error("ntfy.sh POST failed — %s", e)
        return f"ERROR sending briefing: {e}"
    except Exception as e:
        log.error("unexpected error sending briefing — %s", e)
        raise


if __name__ == "__main__":
    mcp.run()
