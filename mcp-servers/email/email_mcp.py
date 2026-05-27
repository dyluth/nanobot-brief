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

import yaml
from mcp.server.fastmcp import FastMCP

CONFIG_PATH = Path("/home/cam/nanobot-brief/config.yaml")
LOG_FILE    = Path("/home/cam/daily-briefings/mcp-debug.log")
IMAP_HOST    = "imap.gmail.com"
IMAP_PORT    = 993
IMAP_TIMEOUT = 30   # seconds; guards against hung TCP connections
HARD_LIMIT   = 3000

# Locale-safe English month abbreviations for IMAP SINCE date format
_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


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
      Phase 2 — Expand: fetch text/plain bodies in recency order, filling a
                hard 3000-character budget. Most recent emails get full bodies;
                older ones are truncated or marked as budget-reached.

    Returns a formatted string (index + bodies) or a descriptive message if
    no accounts are configured or no recent mail was found.
    """
    log.info("get_recent_emails called")
    config = _load_config()
    accounts = config.get("email_accounts", [])

    if not accounts:
        log.warning("no email_accounts configured in config.yaml")
        return "No email accounts configured."

    cutoff = datetime.datetime.now(tz=datetime.timezone.utc) - datetime.timedelta(hours=24)
    all_emails: list[dict] = []
    # Keep connections open for Phase 2 body fetches; keyed by account email string
    connections: dict[str, imaplib.IMAP4_SSL] = {}

    # ── Phase 1: index all accounts ──────────────────────────────────────────
    for acct in accounts:
        email_addr = (acct.get("email") or "").strip()
        app_password = (acct.get("app_password") or "").strip()
        if not email_addr or not app_password:
            log.warning("skipping incomplete email account entry (missing email or app_password)")
            continue

        # Normalize app password: remove ALL whitespace including Unicode non-breaking
        # spaces (\xa0) that are introduced when copy-pasting from web pages.
        # Gmail App Passwords are exactly 16 letters; spaces are cosmetic only.
        app_password = re.sub(r"[\s\u00a0\u200b\u202f\u2009\u2002\u2003\u3000]+", "", app_password)

        log.info("connecting to IMAP for %s", email_addr)
        try:
            mail = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT, timeout=IMAP_TIMEOUT)
            mail.login(email_addr, app_password)
            status, _ = mail.select("inbox", readonly=True)
            if status != "OK":
                log.error("inbox SELECT failed for %s — skipping", email_addr)
                mail.logout()
                continue
            connections[email_addr] = mail
            index = _fetch_account_index(mail, email_addr, cutoff)
            all_emails.extend(index)
        except Exception as exc:
            log.error("connection/index failed for %s: %s", email_addr, exc)

    if not all_emails:
        _close_all(connections)
        log.info("no emails found across all accounts in the last 24h")
        return "No emails in the last 24 hours."

    # Sort globally by most recent first
    all_emails.sort(key=lambda e: e["dt"], reverse=True)
    multi = len(connections) > 1
    log.info("total emails across all accounts: %d (multi-account: %s)", len(all_emails), multi)

    # ── Build index section ───────────────────────────────────────────────────
    index_lines = ["=== Email Index (last 24h) ==="]
    for i, em in enumerate(all_emails, 1):
        local_time = em["dt"].astimezone().strftime("%H:%M")
        from_s = em["from_addr"][:45]
        subj_s = em["subject"][:55]
        acct_tag = f"  [{em['account']}]" if multi else ""
        index_lines.append(f"[{i}] {local_time} | {from_s} | {subj_s}{acct_tag}")
    index_section = "\n".join(index_lines)

    # ── Phase 2: budget-aware body expansion ──────────────────────────────────
    bodies_header = "\n\n=== Bodies ===\n"
    budget = HARD_LIMIT - len(index_section) - len(bodies_header)

    body_chunks: list[str] = []

    for i, em in enumerate(all_emails, 1):
        subj_s = em["subject"][:55]
        section_header = f"--- [{i}] {subj_s} ---\n"

        if budget <= len(section_header):
            # No space left even for the section header — stop
            log.debug("budget exhausted at email [%d]", i)
            break

        mail_conn = connections.get(em["account"])
        if mail_conn is None:
            chunk = section_header + "[connection unavailable]\n"
            body_chunks.append(chunk)
            budget -= len(chunk)
            continue

        log.debug("fetching body for [%d] uid=%s account=%s", i, em["uid"], em["account"])
        try:
            body = _get_plain_body(mail_conn, em["uid"])
        except Exception as exc:
            log.error("body fetch error for [%d]: %s", i, exc)
            body = ""

        if not body:
            chunk = section_header + "[no text content]\n"
        else:
            avail = budget - len(section_header) - 1  # 1 for trailing newline
            if avail <= 0:
                chunk = section_header + "[budget reached]\n"
            elif len(body) <= avail:
                chunk = section_header + body + "\n"
            else:
                ellipsis = "... [truncated]\n"
                chunk = section_header + body[: avail - len(ellipsis)] + ellipsis

        body_chunks.append(chunk)
        budget -= len(chunk)

    _close_all(connections)

    result = index_section + bodies_header + "\n".join(body_chunks)

    # Final safety clamp (should not be needed, but belt-and-suspenders)
    if len(result) > HARD_LIMIT:
        result = result[: HARD_LIMIT - 22] + "\n[hard limit reached]"

    log.info("returning %d chars of email content", len(result))
    return result


def _close_all(connections: dict) -> None:
    """Best-effort logout of all open IMAP connections."""
    for mail in connections.values():
        try:
            mail.logout()
        except Exception:
            pass


if __name__ == "__main__":
    mcp.run()
