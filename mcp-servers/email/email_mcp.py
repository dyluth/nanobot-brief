#!/usr/bin/env python3
"""Email MCP server — exposes get_recent_emails() tool."""

import datetime
import email as email_lib
import imaplib
import logging
import re
import sys
from email.header import decode_header as _hdr_decode
from email.utils import parseaddr, parsedate_to_datetime
from pathlib import Path

import os

import yaml
from mcp.server.fastmcp import FastMCP

CONFIG_PATH = Path("/home/cam/local_agents/nanobot-brief/config.yaml")
IMAP_HOST    = "imap.gmail.com"
IMAP_PORT    = 993
IMAP_TIMEOUT = 30   # seconds; guards against hung TCP connections
HARD_LIMIT   = 3000  # kept for backward-compat get_recent_emails() wrapper

# Locale-safe English month abbreviations for IMAP SINCE date format
_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _setup_logger(name: str) -> logging.Logger:
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
        # (FastMCP already writes INFO+ to stderr via its own Rich logger)
    return log


log = _setup_logger("email")


def _load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


def _decode_header_str(val: str | None) -> str:
    """Decode a possibly RFC2047-encoded header value to plain text."""
    if not val:
        return ""
    parts = []
    for raw, enc in _hdr_decode(val):
        if isinstance(raw, bytes):
            parts.append(raw.decode(enc or "utf-8", errors="replace"))
        else:
            parts.append(str(raw))
    return "".join(parts)


def _parse_dt(date_str: str | None) -> datetime.datetime:
    """Parse an email Date header into a timezone-aware datetime; fall back to epoch."""
    if not date_str:
        return datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc)
    try:
        dt = parsedate_to_datetime(date_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt
    except Exception:
        return datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc)


def _imap_since_date(days_back: int = 1) -> str:
    """Return a locale-safe IMAP SINCE date string, e.g. '26-May-2026'."""
    d = datetime.date.today() - datetime.timedelta(days=days_back)
    return f"{d.day:02d}-{_MONTHS[d.month - 1]}-{d.year}"


def _fetch_account_index(
    mail: imaplib.IMAP4_SSL,
    account_email: str,
    cutoff: datetime.datetime,
) -> list[dict]:
    """
    Phase 1: fetch lightweight metadata (headers only) for recent emails.
    Uses IMAP UID SEARCH + batch BODY.PEEK[HEADER.FIELDS] to minimise round-trips.
    Returns a list of metadata dicts sorted by nothing (caller sorts).
    """
    since_date = _imap_since_date(days_back=1)
    log.debug("UID SEARCH SINCE %s on %s", since_date, account_email)

    status, data = mail.uid("search", None, f"SINCE {since_date}")
    if status != "OK" or not data or not data[0]:
        log.info("no candidate UIDs for %s", account_email)
        return []

    uid_list = data[0].decode().split()
    if not uid_list:
        log.info("empty UID list for %s", account_email)
        return []
    log.info("found %d candidate UIDs for %s", len(uid_list), account_email)

    # Batch-fetch all headers in one IMAP round-trip
    uid_str = ",".join(uid_list)
    status, fetch_data = mail.uid(
        "fetch", uid_str,
        "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])",
    )
    if status != "OK" or not fetch_data:
        log.error("header batch fetch failed for %s", account_email)
        return []

    emails: list[dict] = []
    for part in fetch_data:
        if not isinstance(part, tuple) or len(part) < 2:
            continue

        # Extract UID from the IMAP response descriptor, e.g.:
        # b'5 (UID 123 BODY[HEADER.FIELDS ("FROM" "SUBJECT" "DATE")] {85}'
        descriptor = part[0].decode(errors="replace")
        uid_match = re.search(r"\bUID\s+(\d+)\b", descriptor, re.IGNORECASE)
        uid = uid_match.group(1) if uid_match else None
        if uid is None:
            log.warning("could not extract UID from descriptor: %r", descriptor)
            continue

        header_bytes = part[1]
        msg = email_lib.message_from_bytes(header_bytes)

        dt = _parse_dt(msg.get("Date"))
        # Apply true 24-hour filter: IMAP SINCE is date-only and may return older mail
        if dt < cutoff:
            continue

        from_raw = _decode_header_str(msg.get("From", ""))
        _, addr = parseaddr(from_raw)
        from_display = addr.strip() or from_raw.strip() or "unknown"
        subject = _decode_header_str(msg.get("Subject", "")).strip() or "(no subject)"

        emails.append({
            "uid": uid,          # string, e.g. "123"
            "account": account_email,
            "from_addr": from_display,
            "subject": subject,
            "dt": dt,
        })

    log.info("after 24h filter: %d emails from %s", len(emails), account_email)
    return emails


def _get_plain_body(mail: imaplib.IMAP4_SSL, uid: str) -> str:
    """
    Phase 2: fetch the full RFC822 message and extract text/plain content only.
    Skips HTML parts, attachments, and inline binary data.
    `get_payload(decode=True)` transparently handles base64 / quoted-printable
    transfer encodings so we never see raw base64 blobs.
    """
    status, data = mail.uid("fetch", uid, "(BODY.PEEK[])")
    if status != "OK" or not data:
        return ""

    raw: bytes | None = None
    for part in data:
        if isinstance(part, tuple) and len(part) >= 2:
            raw = part[1]
            break
    if not raw:
        return ""

    msg = email_lib.message_from_bytes(raw)
    text_parts: list[str] = []

    for mime_part in msg.walk():
        # Only plain text, no attachments
        if mime_part.get_content_type() != "text/plain":
            continue
        if "attachment" in str(mime_part.get("Content-Disposition", "")).lower():
            continue

        # get_payload(decode=True) decodes the Content-Transfer-Encoding
        # (base64, quoted-printable, etc.) — result is always raw bytes.
        payload = mime_part.get_payload(decode=True)
        if not payload:
            continue

        charset = mime_part.get_content_charset() or "utf-8"
        try:
            text = payload.decode(charset, errors="replace")
        except (LookupError, UnicodeDecodeError):
            text = payload.decode("utf-8", errors="replace")
        text_parts.append(text)

    body = "\n".join(text_parts).strip()
    # Collapse runs of 3+ blank lines to keep output compact
    body = re.sub(r"\n{3,}", "\n\n", body)
    return body


def _normalise_app_password(pw: str) -> str:
    """Remove all whitespace variants (including Unicode non-breaking spaces)."""
    return re.sub(r"[\s ​    　]+", "", pw)


def _close_all(connections: dict) -> None:
    """Best-effort logout of all open IMAP connections."""
    for mail in connections.values():
        try:
            mail.logout()
        except Exception:
            pass


# ── Public helpers (called directly by briefing.py) ──────────────────────────

def get_email_index() -> tuple[str, list[dict]]:
    """
    Phase 1: fetch lightweight metadata for all recent emails across all accounts.

    Opens IMAP connections, batch-fetches headers from the last 24 hours, then
    closes connections. Returns a (formatted_index_str, email_metadata_list) tuple.

    formatted_index_str — the "=== Email Index (last 24h) ===" section, ready
    to pass to a selector LLM call.

    email_metadata_list — sorted most-recent-first list of dicts:
      {uid, account, from_addr, subject, dt}
    Pass this list (plus your chosen 1-based indices) to get_email_bodies_for().

    Returns ("", []) when no accounts are configured or no recent mail was found.
    """
    log.info("get_email_index called")
    config = _load_config()
    accounts = config.get("email_accounts", [])

    if not accounts:
        log.warning("no email_accounts configured in config.yaml")
        return ("", [])

    cutoff = datetime.datetime.now(tz=datetime.timezone.utc) - datetime.timedelta(hours=24)
    all_emails: list[dict] = []

    for acct in accounts:
        email_addr = (acct.get("email") or "").strip()
        app_password = (acct.get("app_password") or "").strip()
        if not email_addr or not app_password:
            log.warning("skipping incomplete email account entry (missing email or app_password)")
            continue

        app_password = _normalise_app_password(app_password)
        log.info("connecting to IMAP for %s (index phase)", email_addr)
        try:
            mail = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT, timeout=IMAP_TIMEOUT)
            mail.login(email_addr, app_password)
            status, _ = mail.select("inbox", readonly=True)
            if status != "OK":
                log.error("inbox SELECT failed for %s — skipping", email_addr)
                mail.logout()
                continue
            index = _fetch_account_index(mail, email_addr, cutoff)
            all_emails.extend(index)
            mail.logout()
        except Exception as exc:
            log.error("index phase failed for %s: %s", email_addr, exc)

    if not all_emails:
        log.info("no emails found across all accounts in the last 24h")
        return ("", [])

    # Sort by account order in config first (business accounts listed first get priority),
    # then by recency within each account. Prevents a newsletter-heavy inbox from
    # pushing business emails past the per-run check cap.
    acct_order = {(a.get("email") or "").strip(): i for i, a in enumerate(accounts)}
    all_emails.sort(key=lambda e: (acct_order.get(e["account"], 99), -e["dt"].timestamp()))
    multi = len({e["account"] for e in all_emails}) > 1
    log.info("total emails indexed: %d (multi-account: %s)", len(all_emails), multi)

    index_lines = ["=== Email Index (last 24h) ==="]
    for i, em in enumerate(all_emails, 1):
        local_time = em["dt"].astimezone().strftime("%H:%M")
        from_s = em["from_addr"][:45]
        subj_s = em["subject"][:55]
        acct_tag = f"  [{em['account']}]" if multi else ""
        index_lines.append(f"[{i}] {local_time} | {from_s} | {subj_s}{acct_tag}")

    index_str = "\n".join(index_lines)
    return (index_str, all_emails)


def get_email_bodies_for(
    email_list: list[dict],
    indices: list[int],
    budget: int = 15000,
    per_email_limit: int = 3000,
) -> list[tuple[dict, str]]:
    """
    Phase 2: fetch bodies for specific 1-based indices from email_list.

    Reopens IMAP connections only for the accounts needed (lightweight reconnect —
    fine for a cron script). Fetches full RFC822 messages, extracts text/plain only.

    indices         — 1-based indices into email_list (as shown in the Email Index).
    budget          — total character cap across all fetched bodies (default 15000).
    per_email_limit — hard cap per individual email body (default 3000).
                      Prevents one large email from consuming the entire budget and
                      starving all other selected emails. Applied before the total budget
                      check so every email gets a fair share.

    Returns a list of (metadata_dict, body_str) tuples in the order of `indices`.
    body_str is "" if the email has no text content, the fetch failed, or the
    connection was unavailable.

    Out-of-range indices are silently skipped.
    """
    if not indices or not email_list:
        return []

    config = _load_config()
    accounts_map = {
        (acct.get("email") or "").strip(): acct
        for acct in config.get("email_accounts", [])
    }

    # Determine which accounts need connections
    needed_accounts: set[str] = set()
    for idx in indices:
        if 1 <= idx <= len(email_list):
            needed_accounts.add(email_list[idx - 1]["account"])

    # Open connections for needed accounts only
    connections: dict[str, imaplib.IMAP4_SSL] = {}
    for email_addr in needed_accounts:
        acct = accounts_map.get(email_addr)
        if not acct:
            log.warning("no config entry for account %s", email_addr)
            continue
        app_password = _normalise_app_password((acct.get("app_password") or "").strip())
        if not app_password:
            log.warning("no app_password for %s", email_addr)
            continue
        log.info("connecting to IMAP for %s (body phase)", email_addr)
        try:
            mail = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT, timeout=IMAP_TIMEOUT)
            mail.login(email_addr, app_password)
            status, _ = mail.select("inbox", readonly=True)
            if status != "OK":
                log.error("inbox SELECT failed for %s", email_addr)
                mail.logout()
                continue
            connections[email_addr] = mail
        except Exception as exc:
            log.error("body-phase connection failed for %s: %s", email_addr, exc)

    results: list[tuple[dict, str]] = []
    remaining = budget

    for idx in indices:
        if not (1 <= idx <= len(email_list)):
            log.warning("index %d out of range (list has %d)", idx, len(email_list))
            continue

        em = email_list[idx - 1]
        conn = connections.get(em["account"])
        if conn is None:
            log.warning("no connection for %s, skipping [%d]", em["account"], idx)
            results.append((em, ""))
            continue

        if remaining <= 0:
            log.debug("budget exhausted; skipping [%d]", idx)
            results.append((em, ""))
            continue

        log.debug("fetching body for [%d] uid=%s account=%s", idx, em["uid"], em["account"])
        try:
            body = _get_plain_body(conn, em["uid"])
        except Exception as exc:
            log.error("body fetch error for [%d]: %s", idx, exc)
            body = ""

        # Per-email cap: clamp each body independently before touching total budget
        if body and len(body) > per_email_limit:
            ellipsis = "... [truncated]"
            body = body[: per_email_limit - len(ellipsis)] + ellipsis

        # Total budget check: stop adding bodies once the aggregate limit is reached
        if body and len(body) > remaining:
            ellipsis = "... [truncated]"
            body = body[: remaining - len(ellipsis)] + ellipsis

        if body:
            remaining -= len(body)

        results.append((em, body))

    _close_all(connections)
    log.info("body phase complete: %d/%d emails fetched", len(results), len(indices))
    return results


# ── MCP tool (backward-compatible) ───────────────────────────────────────────

log.info("email MCP server started")
mcp = FastMCP("email")


@mcp.tool()
def get_recent_emails() -> str:
    """
    Fetch emails received in the last 24 hours from all configured IMAP accounts.

    Two-phase approach:
      Phase 1 — Index: fetch sender/subject/time for ALL recent emails across
                all accounts (lightweight header-only fetch, single round-trip
                per account).
      Phase 2 — Expand: fetch text/plain bodies for the top 3 most recent emails
                within a hard 3000-character budget.

    Returns a formatted string (index + bodies) or a descriptive message if
    no accounts are configured or no recent mail was found.

    For smarter LLM-driven selection, call get_email_index() and
    get_email_bodies_for() directly from briefing.py instead.
    """
    log.info("get_recent_emails called (backward-compat wrapper)")
    index_str, email_list = get_email_index()

    if not email_list:
        return "No emails in the last 24 hours."

    # Default: expand top 3 by recency
    default_indices = list(range(1, min(4, len(email_list) + 1)))
    body_results = get_email_bodies_for(email_list, default_indices, budget=HARD_LIMIT)

    if not body_results:
        return index_str

    bodies_header = "\n\n=== Bodies ===\n"
    body_chunks: list[str] = []
    for orig_idx, (em, body) in zip(default_indices, body_results):
        subj_s = em["subject"][:55]
        section_header = f"--- [{orig_idx}] {subj_s} ---\n"
        if not body:
            body_chunks.append(section_header + "[no text content]\n")
        else:
            body_chunks.append(section_header + body + "\n")

    result = index_str + bodies_header + "\n".join(body_chunks)

    # Final safety clamp (belt-and-suspenders)
    if len(result) > HARD_LIMIT:
        result = result[: HARD_LIMIT - 22] + "\n[hard limit reached]"

    log.info("returning %d chars of email content", len(result))
    return result


if __name__ == "__main__":
    mcp.run()
