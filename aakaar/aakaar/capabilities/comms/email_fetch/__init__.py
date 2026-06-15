"""cap.email_fetch — fetch recent messages from an IMAP mailbox.

Connects to an IMAP server over TLS using credentials from the tenant's
vault (under a ``(tenant, cap.email_fetch, account_alias)`` grant),
searches a mailbox with optional subject/sender/since filters, and
returns lightweight message summaries (uid, subject, from, date and a
short plaintext body preview). It does NOT download attachments or mark
messages as read — the fetch uses ``BODY.PEEK`` so server-side
``\\Seen`` flags are left untouched.

Connection + identity live on the vault grant, not on the DAG:

  host:     required. IMAP server hostname (e.g. imap.gmail.com).
  port:     optional, default 993 (implicit TLS).
  username: required. Mailbox login.
  imap_password: required. Mailbox password / app password.

Host and port may also be supplied via the grant's ``input_defaults``
(non-secret config); values in the vault entry take precedence.

Inputs:

  account_alias:    which credential set to use.
  mailbox:          IMAP folder to open, default "INBOX".
  subject_contains: optional case-insensitive SUBJECT substring filter.
  from_contains:    optional case-insensitive FROM substring filter.
  since:            optional YYYY-MM-DD; only messages on/after this date
                    (IMAP SINCE is date-granular, server-local).
  limit:            max messages to return, newest first (1..200, def 20).

Returns ``{messages: [{uid, subject, from_, date, body_preview}]}``.

stdlib only (imaplib + email), imported lazily so module import never
fails on a slim build.
"""

from __future__ import annotations

import logging
from email.message import Message
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from aakaar.interpreter.activities.types import ActivityContext
from aakaar.interpreter.credentials import fetch_credentials
from aakaar.shared.registry import CapabilityDefinition, SecretSpec

logger = logging.getLogger(__name__)
CAP_REF = "cap.email_fetch"

_DEFAULT_PORT = 993
_DEFAULT_LIMIT = 20
_BODY_PREVIEW_CHARS = 500


class _Inputs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    account_alias: str = Field(
        description="Which credential set to use, e.g. 'primary'. The grant must exist."
    )
    mailbox: str = Field(
        default="INBOX",
        description="IMAP folder/mailbox to open.",
    )
    subject_contains: str | None = Field(
        default=None,
        description="Case-insensitive substring the subject must contain.",
    )
    from_contains: str | None = Field(
        default=None,
        description="Case-insensitive substring the From header must contain.",
    )
    since: str | None = Field(
        default=None,
        description=(
            "Only return messages dated on/after this day, as YYYY-MM-DD. "
            "IMAP SINCE is date-granular and evaluated server-side."
        ),
    )
    limit: int = Field(
        default=_DEFAULT_LIMIT,
        ge=1,
        le=200,
        description="Maximum number of messages to return, newest first.",
    )

    @field_validator("since")
    @classmethod
    def _validate_since(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        if not v:
            return None
        # Validate shape here so a bad date fails the node at input
        # binding rather than producing a confusing IMAP SEARCH error.
        import datetime as _dt

        try:
            _dt.datetime.strptime(v, "%Y-%m-%d")
        except ValueError as e:
            raise ValueError(
                f"since must be YYYY-MM-DD, got {v!r}"
            ) from e
        return v


class _Message(BaseModel):
    uid: str = Field(description="IMAP UID of the message (stable within the mailbox).")
    subject: str = Field(description="Decoded Subject header (may be empty).")
    from_: str = Field(description="Decoded From header (may be empty).")
    date: str = Field(description="Raw Date header (may be empty).")
    body_preview: str = Field(
        description="First chunk of the plaintext body, whitespace-collapsed."
    )


class _Outputs(BaseModel):
    messages: list[_Message] = Field(
        description="Matching messages, newest first, capped at `limit`."
    )


definition = CapabilityDefinition(
    ref=CAP_REF,
    description=(
        "Fetch recent messages from an IMAP mailbox using stored credentials, "
        "with optional subject/sender/since filters. Returns lightweight "
        "summaries (uid, subject, from, date, body preview) newest-first. "
        "Read-only: uses BODY.PEEK so messages are not marked as read, and "
        "attachments are not downloaded. Host/port live on the grant."
    ),
    input_schema=_Inputs,
    output_schema=_Outputs,
    secrets=(
        SecretSpec(name="username", description="IMAP mailbox login."),
        SecretSpec(name="imap_password", description="IMAP mailbox password or app password."),
        SecretSpec(
            name="host",
            description=(
                "IMAP server hostname. May instead be set on the grant's "
                "input_defaults; vault value wins."
            ),
        ),
        SecretSpec(
            name="port",
            description=(
                "IMAP server port (default 993). Optional; may be set on the "
                "grant's input_defaults instead."
            ),
        ),
    ),
    tags=("comms", "email", "imap", "fetch"),
)


# --------------------------------------------------------------------------
# Pure helpers (unit-tested without a live server)
# --------------------------------------------------------------------------


def _decode_header(raw: str | None) -> str:
    """Decode an RFC 2047 encoded-word header into a plain str.

    imaplib hands back header bytes as latin-1-ish str; `email.header`
    splits the encoded words and tells us their charsets. We stitch the
    pieces back into a single unicode string, falling back to a lenient
    decode when a part claims an unknown/garbled charset.
    """
    if not raw:
        return ""
    from email.header import decode_header

    parts: list[str] = []
    for chunk, enc in decode_header(raw):
        if isinstance(chunk, bytes):
            try:
                parts.append(chunk.decode(enc or "utf-8", errors="replace"))
            except (LookupError, ValueError):
                parts.append(chunk.decode("utf-8", errors="replace"))
        else:
            parts.append(chunk)
    return _collapse_ws("".join(parts))


def _collapse_ws(text: str) -> str:
    """Collapse runs of whitespace (incl. newlines) into single spaces."""
    return " ".join(text.split())


def _build_search_criteria(
    *,
    subject_contains: str | None,
    from_contains: str | None,
    since: str | None,
) -> list[str]:
    """Translate the inputs into IMAP SEARCH tokens.

    Substring filters use IMAP's SUBJECT/FROM which already match on
    substrings server-side; SINCE takes the IMAP date form (DD-Mon-YYYY).
    Returns ['ALL'] when no filter is set, since IMAP SEARCH needs at
    least one criterion.
    """
    crit: list[str] = []
    if since:
        crit += ["SINCE", _imap_date(since)]
    if subject_contains:
        crit += ["SUBJECT", subject_contains]
    if from_contains:
        crit += ["FROM", from_contains]
    return crit or ["ALL"]


def _imap_date(ymd: str) -> str:
    """YYYY-MM-DD -> IMAP SEARCH date 'DD-Mon-YYYY' (e.g. 02-Jan-2026)."""
    import datetime as _dt

    d = _dt.datetime.strptime(ymd.strip(), "%Y-%m-%d")
    return d.strftime("%d-%b-%Y")


def _extract_body_preview(msg: Any, *, limit: int = _BODY_PREVIEW_CHARS) -> str:
    """Pull a short plaintext preview from an `email.message.Message`.

    Prefers the first ``text/plain`` part (skipping attachments). Falls
    back to a ``text/html`` part with tags crudely stripped, then to the
    raw payload. Always whitespace-collapsed and truncated to `limit`.
    """
    plain = _first_text_payload(msg, "text/plain")
    if plain is None:
        html = _first_text_payload(msg, "text/html")
        plain = _strip_html(html) if html is not None else ""
    return _collapse_ws(plain)[:limit]


def _first_text_payload(msg: Any, want_type: str) -> str | None:
    """Return the decoded text of the first part matching `want_type`,
    skipping attachment dispositions. None if no such part exists."""
    if msg.is_multipart():
        for part in msg.walk():
            if part.is_multipart():
                continue
            disp = str(part.get("Content-Disposition") or "").lower()
            if "attachment" in disp:
                continue
            if part.get_content_type() == want_type:
                got = _decode_part_text(part)
                if got is not None:
                    return got
        return None
    ctype = msg.get_content_type()
    if ctype == want_type:
        return _decode_part_text(msg)
    # Single-part message with an unexpected/missing type: treat as text
    # when text/plain was requested so a bare message still yields a body.
    # Skip text/html here so the HTML fallback path can strip its tags.
    if want_type == "text/plain" and ctype != "text/html":
        return _decode_part_text(msg)
    return None


def _decode_part_text(part: Message) -> str | None:
    payload = part.get_payload(decode=True)
    if not isinstance(payload, (bytes, bytearray)):
        return None
    charset = part.get_content_charset() or "utf-8"
    try:
        return str(payload.decode(charset, errors="replace"))
    except (LookupError, ValueError):
        return str(payload.decode("utf-8", errors="replace"))


def _strip_html(html: str) -> str:
    """Very small HTML-to-text: drop tags, decode entities. Good enough
    for a preview; we never round-trip this back into markup."""
    import html as _html
    import re

    no_tags = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", html)
    no_tags = re.sub(r"(?s)<[^>]+>", " ", no_tags)
    return _html.unescape(no_tags)


def _summarize_message(uid: str, raw_bytes: bytes) -> dict[str, str]:
    """Parse a raw RFC822 message into the summary dict we return."""
    from email import message_from_bytes
    from email.policy import default as default_policy

    msg = message_from_bytes(raw_bytes, policy=default_policy)
    return {
        "uid": uid,
        "subject": _decode_header(_header_str(msg, "Subject")),
        "from_": _decode_header(_header_str(msg, "From")),
        "date": _collapse_ws(_header_str(msg, "Date")),
        "body_preview": _extract_body_preview(msg),
    }


def _header_str(msg: Any, name: str) -> str:
    val = msg.get(name)
    return "" if val is None else str(val)


# --------------------------------------------------------------------------
# Handler
# --------------------------------------------------------------------------


async def handler(ctx: ActivityContext, inputs: dict[str, Any]) -> dict[str, Any]:
    import asyncio
    import imaplib  # noqa: F401  (imported lazily; used inside _fetch_sync)

    alias = inputs["account_alias"]
    mailbox = inputs.get("mailbox") or "INBOX"
    subject_contains = inputs.get("subject_contains")
    from_contains = inputs.get("from_contains")
    since = inputs.get("since")
    limit = int(inputs.get("limit", _DEFAULT_LIMIT))

    creds = fetch_credentials(ctx, capability_ref=CAP_REF, account_alias=alias)
    username = (creds.get("username") or "").strip()
    password = creds.get("imap_password") or creds.get("password") or ""
    if not username or not password:
        raise PermissionError(
            f"cap.email_fetch: vault entry for alias {alias!r} must have "
            f"`username` and `imap_password`"
        )

    grant_defaults = (
        (ctx.granted_capabilities.get(CAP_REF) or {}).get(alias) or {}
    ).get("input_defaults") or {}
    host = (creds.get("host") or grant_defaults.get("host") or "").strip()
    if not host:
        raise RuntimeError(
            f"cap.email_fetch: no IMAP host for alias {alias!r}; set `host` "
            f"on the vault entry or the grant's input_defaults"
        )
    port = int(creds.get("port") or grant_defaults.get("port") or _DEFAULT_PORT)

    criteria = _build_search_criteria(
        subject_contains=subject_contains,
        from_contains=from_contains,
        since=since,
    )

    logger.info(
        "cap.email_fetch start run_id=%s alias=%s host=%s port=%d mailbox=%s "
        "criteria=%s limit=%d",
        ctx.run_id,
        alias,
        host,
        port,
        mailbox,
        criteria,
        limit,
    )

    # imaplib is blocking; run the whole session in a worker thread so we
    # don't stall the event loop.
    messages = await asyncio.to_thread(
        _fetch_sync,
        host=host,
        port=port,
        username=username,
        password=password,
        mailbox=mailbox,
        criteria=criteria,
        limit=limit,
    )

    logger.info(
        "cap.email_fetch ok run_id=%s alias=%s mailbox=%s returned=%d",
        ctx.run_id,
        alias,
        mailbox,
        len(messages),
    )
    return {"messages": messages}


def _fetch_sync(
    *,
    host: str,
    port: int,
    username: str,
    password: str,
    mailbox: str,
    criteria: list[str],
    limit: int,
) -> list[dict[str, str]]:
    """Blocking IMAP session: connect, search, fetch summaries. Run via
    asyncio.to_thread from the handler. Never logs credentials."""
    import imaplib

    conn = imaplib.IMAP4_SSL(host=host, port=port)
    try:
        conn.login(username, password)
        typ, _ = conn.select(mailbox, readonly=True)
        if typ != "OK":
            raise RuntimeError(
                f"cap.email_fetch: could not select mailbox {mailbox!r} on {host}"
            )

        # No CHARSET argument (imaplib skips a None charset entirely, so
        # omitting it yields the identical `UID SEARCH <criteria>` command).
        typ, data = conn.uid("search", *criteria)
        if typ != "OK":
            raise RuntimeError(
                f"cap.email_fetch: IMAP SEARCH failed on {host} mailbox {mailbox!r}"
            )
        raw_ids = data[0].split() if data and data[0] else []
        # Newest first, capped at `limit`.
        selected = list(reversed(raw_ids))[:limit]

        out: list[dict[str, str]] = []
        for uid_bytes in selected:
            uid = uid_bytes.decode("ascii", errors="replace")
            # BODY.PEEK leaves the \Seen flag alone (read-only fetch).
            typ, fetched = conn.uid("fetch", uid, "(BODY.PEEK[])")
            if typ != "OK" or not fetched:
                logger.warning(
                    "cap.email_fetch: fetch failed for uid=%s mailbox=%s", uid, mailbox
                )
                continue
            raw_bytes = _first_rfc822_payload(fetched)
            if raw_bytes is None:
                continue
            out.append(_summarize_message(uid, raw_bytes))
        return out
    finally:
        import contextlib

        # Best-effort teardown: mailbox may not be selected, and a broken
        # connection makes logout raise. Neither should mask a real error.
        with contextlib.suppress(Exception):
            conn.close()
        with contextlib.suppress(Exception):
            conn.logout()


def _first_rfc822_payload(fetched: list[Any]) -> bytes | None:
    """imaplib FETCH returns a list mixing tuples (header, body-bytes) and
    bare bytes (closing parens). Pull the first body payload out of it."""
    for item in fetched:
        if isinstance(item, tuple) and len(item) >= 2 and isinstance(item[1], (bytes, bytearray)):
            return bytes(item[1])
    return None
