"""cap.email_send — send an email over SMTP using stdlib smtplib + email.

Connects to an SMTP server with credentials from the tenant's vault
(under a `(tenant, cap.email_send, account_alias)` grant), builds a MIME
message (plain text and/or HTML, with optional attachments pulled from
the object store), and sends it.

Connection config lives on the grant — host/port/username on the grant's
`input_defaults`, the password as a vault secret — not on the DAG. The
DAG only carries the message: recipients, subject, body, attachments.

Grant `input_defaults`:

  host: required. SMTP server hostname or IP.
  port: optional, default 587 (STARTTLS). 465 implies implicit TLS.
  use_tls: optional bool. Implicit TLS from connect (SMTP-over-SSL).
    Defaults to True when port == 465, else False.
  use_starttls: optional bool. Upgrade the plaintext connection with
    STARTTLS after EHLO. Defaults to True unless use_tls is set.
  from_addr: optional. Envelope/From header address. Defaults to the
    grant's `username` when it looks like an address.
  timeout_s: optional, default 30.
  allow_private: optional bool, default True (airgapped LANs are the
    common deployment; SMTP relays usually live on the local network).
  recipient_allowlist: optional list of addresses and/or "@domain"
    entries. When set, every envelope recipient (To + Cc) must match one,
    or the send is refused — a guard against a misplanned/injected flow
    mailing data to an arbitrary mailbox.

Vault secrets:

  username: required. SMTP login user.
  smtp_password: required. SMTP login password.

Inputs (on the DAG):

  account_alias: which grant to use.
  to: list of recipient addresses (required, non-empty).
  subject: subject line.
  body: plain-text body.
  html: optional HTML body (sent as a multipart/alternative alongside
    the plain body).
  cc: optional list of carbon-copy addresses.
  attachments: optional list of `aakaar://` object-store URIs; each is
    fetched and attached with its user-facing basename.

Returns `{sent: True, to: [...recipients including cc...]}`. The send
itself is blocking stdlib I/O, so it runs in a worker thread to avoid
stalling the executor's event loop.
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from aakaar.core.net.ssrf import assert_host_allowed
from aakaar.interpreter.activities.types import ActivityContext
from aakaar.interpreter.credentials import fetch_credentials
from aakaar.shared.registry import CapabilityDefinition, SecretSpec
from aakaar.storage.object_store import URI_PREFIX, parse_uri

logger = logging.getLogger(__name__)
CAP_REF = "cap.email_send"

_DEFAULT_PORT = 587
_IMPLICIT_TLS_PORT = 465
_DEFAULT_TIMEOUT_S = 30


class _Inputs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    account_alias: str = Field(
        description="Which credential set / grant to use, e.g. 'primary'."
    )
    to: list[str] = Field(
        min_length=1,
        description="Recipient email addresses. At least one is required.",
    )
    subject: str = Field(default="", description="Subject line.")
    body: str = Field(default="", description="Plain-text message body.")
    html: str | None = Field(
        default=None,
        description=(
            "Optional HTML body. When set it is sent as a multipart/alternative "
            "alongside the plain-text body so clients can pick either."
        ),
    )
    cc: list[str] | None = Field(
        default=None, description="Optional carbon-copy recipient addresses."
    )
    attachments: list[str] | None = Field(
        default=None,
        description=(
            "Optional list of aakaar:// object-store URIs to attach. Each file "
            "is fetched from the object store and attached by its basename."
        ),
    )


class _Outputs(BaseModel):
    sent: bool = Field(description="True when the message was handed to the SMTP server.")
    to: list[str] = Field(description="All envelope recipients (To + Cc).")


definition = CapabilityDefinition(
    ref=CAP_REF,
    description=(
        "Send an email over SMTP using stored credentials. Host/port/username "
        "live on the grant's input_defaults; the password is a vault secret. "
        "Supports plain-text and HTML bodies plus attachments pulled from the "
        "object store. A grant-level recipient_allowlist (addresses and/or "
        "@domains), when set, restricts who can be mailed."
    ),
    input_schema=_Inputs,
    output_schema=_Outputs,
    secrets=(
        SecretSpec(name="username", description="SMTP login username."),
        SecretSpec(name="smtp_password", description="SMTP login password."),
    ),
    tags=("comms", "email", "smtp", "notify"),
)


# ---------- pure helpers ---------------------------------------------------


def _user_facing_basename(uri: str) -> str:
    """Recover a human-friendly filename from an object-store key.

    Storage keys follow the `<uuid32hex>_<original>` shape used by
    cap.file_download / file.read_local. Strip the uuid prefix when
    present so attachments keep their recognisable name.
    """
    if not uri:
        return "attachment.bin"
    tail = uri.rstrip("/").rsplit("/", 1)[-1]
    if not tail:
        return "attachment.bin"
    if "_" in tail:
        head, rest = tail.split("_", 1)
        if len(head) == 32 and all(c in "0123456789abcdef" for c in head.lower()) and rest:
            return rest
    return tail


def _clean_addr_list(values: list[str] | None) -> list[str]:
    """Trim, drop empties, de-dupe (preserving order)."""
    out: list[str] = []
    seen: set[str] = set()
    for v in values or []:
        addr = (v or "").strip()
        if addr and addr not in seen:
            seen.add(addr)
            out.append(addr)
    return out


def _bare_addresses(value: str) -> list[str]:
    """Every addr-spec in a recipient string, lowercased.

    Uses getaddresses (not parseaddr) so a single string smuggling a comma-
    separated list — e.g. 'attacker@evil.test, victim@corp.example' — is
    expanded into all its addresses rather than collapsed to one. smtplib
    feeds each to_addrs element verbatim to RCPT TO, so a permissive MTA would
    deliver to every smuggled address; the allowlist must therefore vet each.
    A string with no parseable address falls back to itself so it still fails
    a non-empty allowlist instead of silently passing.
    """
    from email.utils import getaddresses

    addrs = [addr.strip().lower() for _name, addr in getaddresses([value]) if addr]
    return addrs or [value.strip().lower()]


def assert_recipients_allowed(
    recipients: list[str], allowlist: list[str] | None
) -> None:
    """Enforce the grant's recipient allowlist (defense against a planner or
    prompt-injected flow exfiltrating data to an arbitrary mailbox).

    Allowlist entries are matched case-insensitively against each recipient's
    bare address: an entry starting with '@' allows its whole domain
    ('@corp.example'), anything else must equal the full address. Every address
    a recipient string expands to must match (a single string may pack several
    via comma syntax). An empty or absent allowlist permits all recipients (the
    grant opted out).
    """
    entries = [e.strip().lower() for e in allowlist or [] if e and e.strip()]
    if not entries:
        return
    domains = {e for e in entries if e.startswith("@")}
    exact = {e for e in entries if not e.startswith("@")}
    for recipient in recipients:
        for addr in _bare_addresses(recipient):
            domain = addr[addr.rfind("@") :] if "@" in addr else ""
            if addr in exact or domain in domains:
                continue
            raise PermissionError(
                f"cap.email_send: recipient {recipient!r} is not permitted by "
                f"the grant's recipient_allowlist"
            )


def build_message(
    *,
    from_addr: str,
    to: list[str],
    subject: str,
    body: str,
    html: str | None = None,
    cc: list[str] | None = None,
    attachments: list[tuple[str, bytes]] | None = None,
) -> Any:
    """Build an `email.message.EmailMessage`.

    Pure (no I/O): callers pass already-fetched attachment bytes as
    (filename, data) pairs. Kept importable for unit tests that assert on
    the rendered MIME without touching a live SMTP server.
    """
    import mimetypes
    from email.message import EmailMessage

    msg = EmailMessage()
    msg["From"] = from_addr
    msg["To"] = ", ".join(to)
    if cc:
        msg["Cc"] = ", ".join(cc)
    msg["Subject"] = subject or ""

    # Body: set the plain part first; add HTML as an alternative if given.
    msg.set_content(body or "")
    if html:
        msg.add_alternative(html, subtype="html")

    for filename, data in attachments or []:
        ctype, _enc = mimetypes.guess_type(filename)
        if ctype and "/" in ctype:
            maintype, subtype = ctype.split("/", 1)
        else:
            maintype, subtype = "application", "octet-stream"
        msg.add_attachment(
            data, maintype=maintype, subtype=subtype, filename=filename
        )
    return msg


# ---------- handler --------------------------------------------------------


def _grant_defaults(ctx: ActivityContext, alias: str) -> dict[str, Any]:
    return (
        (ctx.granted_capabilities.get(CAP_REF) or {}).get(alias) or {}
    ).get("input_defaults") or {}


def _send(
    *,
    host: str,
    port: int,
    username: str,
    password: str,
    use_tls: bool,
    use_starttls: bool,
    timeout_s: int,
    from_addr: str,
    recipients: list[str],
    msg: Any,
) -> None:
    """Blocking SMTP send. Runs in a worker thread via the handler."""
    import smtplib
    import ssl

    context = ssl.create_default_context()
    if use_tls:
        with smtplib.SMTP_SSL(
            host, port, timeout=timeout_s, context=context
        ) as server:
            server.login(username, password)
            server.send_message(msg, from_addr=from_addr, to_addrs=recipients)
    else:
        with smtplib.SMTP(host, port, timeout=timeout_s) as server:
            server.ehlo()
            if use_starttls:
                server.starttls(context=context)
                server.ehlo()
            server.login(username, password)
            server.send_message(msg, from_addr=from_addr, to_addrs=recipients)


async def handler(ctx: ActivityContext, inputs: dict[str, Any]) -> dict[str, Any]:
    import asyncio

    alias = inputs["account_alias"]

    to = _clean_addr_list(inputs.get("to"))
    if not to:
        raise ValueError("cap.email_send: `to` must contain at least one address")
    cc = _clean_addr_list(inputs.get("cc"))
    recipients = list(dict.fromkeys(to + cc))

    subject = inputs.get("subject") or ""
    body = inputs.get("body") or ""
    html = inputs.get("html") or None

    defaults = _grant_defaults(ctx, alias)
    host = (defaults.get("host") or "").strip()
    if not host:
        raise RuntimeError(
            f"cap.email_send: no SMTP host for alias {alias!r}; set `host` on "
            f"the grant's input_defaults"
        )
    port = int(defaults.get("port") or _DEFAULT_PORT)
    timeout_s = int(defaults.get("timeout_s") or _DEFAULT_TIMEOUT_S)
    allow_private = bool(defaults.get("allow_private", True))

    # TLS mode: implicit TLS (465) vs STARTTLS upgrade (587). Honour
    # explicit overrides; otherwise infer from the port.
    use_tls = (
        bool(defaults.get("use_tls"))
        if "use_tls" in defaults
        else port == _IMPLICIT_TLS_PORT
    )
    use_starttls = (
        bool(defaults.get("use_starttls"))
        if "use_starttls" in defaults
        else not use_tls
    )

    # SSRF guard. allow_private defaults True because SMTP relays usually
    # live on the airgapped LAN; a grant can flip it off to force public.
    assert_host_allowed(host, allow_private=allow_private)

    # Grant-level recipient allowlist (optional): when set, every envelope
    # recipient must match an allowed address or @domain.
    assert_recipients_allowed(recipients, defaults.get("recipient_allowlist"))

    creds = fetch_credentials(ctx, capability_ref=CAP_REF, account_alias=alias)
    username = (creds.get("username") or "").strip()
    password = creds.get("smtp_password") or ""
    if not username:
        raise PermissionError(
            f"cap.email_send: vault entry for alias {alias!r} has no `username`"
        )
    if not password:
        raise PermissionError(
            f"cap.email_send: vault entry for alias {alias!r} has no `smtp_password`"
        )

    from_addr = (defaults.get("from_addr") or "").strip()
    if not from_addr:
        from_addr = username if "@" in username else ""
    if not from_addr:
        raise RuntimeError(
            f"cap.email_send: no From address for alias {alias!r}; set "
            f"`from_addr` on the grant's input_defaults (the username is not "
            f"an email address)"
        )

    # Fetch attachments from the object store (refuse non-managed URIs).
    attachments: list[tuple[str, bytes]] = []
    for uri in inputs.get("attachments") or []:
        if not isinstance(uri, str) or not uri.startswith(URI_PREFIX):
            raise ValueError(
                f"cap.email_send: attachment must be an aakaar:// managed-storage "
                f"URI, got {uri!r}"
            )
        parse_uri(uri)  # validate shape early
        data = ctx.object_store.get(uri)
        attachments.append((_user_facing_basename(uri), data))

    msg = build_message(
        from_addr=from_addr,
        to=to,
        subject=subject,
        body=body,
        html=html,
        cc=cc,
        attachments=attachments,
    )

    logger.info(
        "cap.email_send start run_id=%s alias=%s host=%s port=%d "
        "recipients=%d attachments=%d tls=%s starttls=%s",
        ctx.run_id,
        alias,
        host,
        port,
        len(recipients),
        len(attachments),
        use_tls,
        use_starttls,
    )

    await asyncio.to_thread(
        _send,
        host=host,
        port=port,
        username=username,
        password=password,
        use_tls=use_tls,
        use_starttls=use_starttls,
        timeout_s=timeout_s,
        from_addr=from_addr,
        recipients=recipients,
        msg=msg,
    )

    logger.info(
        "cap.email_send ok run_id=%s alias=%s recipients=%d",
        ctx.run_id,
        alias,
        len(recipients),
    )
    return {"sent": True, "to": recipients}
