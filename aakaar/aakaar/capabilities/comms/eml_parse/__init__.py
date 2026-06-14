"""cap.eml_parse — structural parse of a raw RFC822/EML message.

Server-local, no-network, no-secrets, deterministic (no LLM). It takes a
raw email — either an ``aakaar://`` object the upstream graph stored
(e.g. a ``.eml`` pulled by ``cap.file_download`` or an SFTP read) or an
inline RFC822 string — and decomposes it with the stdlib ``email``
package:

  - envelope headers: subject, from_, to, cc, date, message_id
  - every other header, joined per name (Received et al. keep all values)
  - the best text/plain and text/html bodies (multipart/alternative aware)
  - attachment metadata; by default each attachment's decoded bytes are
    written to the object store under a per-run prefix and returned as
    ``aakaar://`` URIs so downstream nodes (cap.pdf_extract,
    cap.doc_extract, ...) can consume them.

This is the structural sibling of ``cap.email_parse`` (which does
LLM/heuristic extraction over an already-plain text body): eml_parse
answers "what is in this message", email_parse answers "what does it
mean".

Bomb defense: a raw message larger than ``_MAX_SOURCE_BYTES`` is refused
before it is parsed (so a giant inline body or deeply nested attachment-free
multipart can't be materialized in full first), and messages with more than
``_MAX_ATTACHMENTS`` attachments, or whose decoded attachments total more
than ``_MAX_TOTAL_ATTACHMENT_BYTES``, are refused with a clear error —
base64 in a multi-part message is an easy amplification vector.
"""

from __future__ import annotations

import logging
import posixpath
import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from aakaar.interpreter.activities.types import ActivityContext
from aakaar.shared.registry import CapabilityDefinition

logger = logging.getLogger(__name__)
CAP_REF = "cap.eml_parse"

_MAX_ATTACHMENTS = 50
_MAX_TOTAL_ATTACHMENT_BYTES = 64 * 1024 * 1024  # 64 MiB decoded, per message
# Cap on the raw message bytes accepted *before* parsing. message_from_bytes
# materializes the whole MIME tree in memory, and the attachment caps above
# only bind on decoded attachments afterward — so a giant inline body or a
# deeply nested attachment-free multipart would otherwise be parsed in full
# first. Mirrors cap.data_transform's _MAX_SOURCE_BYTES guard.
_MAX_SOURCE_BYTES = 64 * 1024 * 1024  # 64 MiB raw, per message


class _Inputs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: str | None = Field(
        default=None,
        description=(
            "aakaar:// URI of the stored raw message (.eml / RFC822 bytes). "
            "Exactly one of source/raw is required."
        ),
    )
    raw: str | None = Field(
        default=None,
        description=(
            "The raw RFC822 message text inline. Exactly one of source/raw "
            "is required."
        ),
    )
    store_attachments: bool = Field(
        default=True,
        description=(
            "Write each attachment's decoded bytes to the object store and "
            "return its aakaar:// URI. When false only metadata is returned "
            "(uri is null)."
        ),
    )

    @model_validator(mode="after")
    def _exactly_one_input(self) -> _Inputs:
        if bool(self.source) == bool(self.raw):
            raise ValueError("exactly one of `source` or `raw` is required")
        return self


class _Attachment(BaseModel):
    filename: str = Field(description="Declared filename (sanitized basename).")
    content_type: str = Field(description="MIME type, e.g. application/pdf.")
    size: int = Field(description="Decoded size in bytes.")
    uri: str | None = Field(
        default=None,
        description="aakaar:// URI of the stored bytes; null when store_attachments=false.",
    )


class _Outputs(BaseModel):
    subject: str = Field(description="Subject header ('' when absent).")
    from_: str = Field(description="From header ('' when absent).")
    to: list[str] = Field(description="Parsed To addresses.")
    cc: list[str] = Field(description="Parsed Cc addresses.")
    date: str | None = Field(default=None, description="Date header as sent.")
    message_id: str | None = Field(default=None, description="Message-ID header.")
    headers: dict[str, str] = Field(
        description=(
            "All headers by name (lowercased); repeated headers are joined "
            "with newlines."
        ),
    )
    text: str | None = Field(
        default=None, description="Best text/plain body, decoded. Null when absent."
    )
    html: str | None = Field(
        default=None, description="Best text/html body, decoded. Null when absent."
    )
    attachments: list[_Attachment] = Field(
        description="Attachment metadata (and stored URIs) in message order.",
    )


definition = CapabilityDefinition(
    ref=CAP_REF,
    description=(
        "Parse a raw RFC822/EML message (from the object store or inline) "
        "into structured parts: envelope headers, all headers, text and HTML "
        "bodies, and attachments — attachment bytes are stored to the object "
        "store and returned as aakaar:// URIs for downstream extraction. "
        "Deterministic stdlib parsing, no LLM, no network, no credentials; "
        "refuses attachment bombs (count and total decoded size capped)."
    ),
    input_schema=_Inputs,
    output_schema=_Outputs,
    secrets=(),
    tags=("comms", "email", "eml", "rfc822", "parse", "attachments"),
)


# ---------------------------------------------------------------------------
# Pure helpers (no ctx) — unit-testable without an ActivityContext.
# ---------------------------------------------------------------------------


def _safe_filename(name: str | None, ordinal: int) -> str:
    """A storage-safe basename for an attachment.

    Strips any path components a hostile message might smuggle into the
    filename parameter and falls back to a stable placeholder.
    """
    base = posixpath.basename((name or "").replace("\\", "/")).strip()
    if base in ("", ".", ".."):
        base = f"attachment-{ordinal:02d}.bin"
    return base


def _address_list(msg: Any, header: str) -> list[str]:
    """Parse a recipient header into display addresses ('Ada <a@x.io>' or bare)."""
    from email.utils import formataddr, getaddresses

    values = [str(v) for v in (msg.get_all(header) or [])]
    out: list[str] = []
    for name, addr in getaddresses(values):
        if not addr:
            continue
        out.append(formataddr((name, addr)) if name else addr)
    return out


def _all_headers(msg: Any) -> dict[str, str]:
    """Every header keyed by lowercase name; repeats joined with newlines."""
    out: dict[str, str] = {}
    for key, value in msg.items():
        name = key.lower()
        text = str(value)
        out[name] = f"{out[name]}\n{text}" if name in out else text
    return out


def _body_text(msg: Any, subtype: str) -> str | None:
    """The preferred body of the given subtype ('plain'/'html'), decoded."""
    part = msg.get_body(preferencelist=(subtype,))
    if part is None:
        return None
    try:
        content = part.get_content()
    except Exception:
        logger.warning(
            "cap.eml_parse: failed to decode %s body", subtype, exc_info=True
        )
        return None
    return content if isinstance(content, str) else None


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


async def handler(ctx: ActivityContext, inputs: dict[str, Any]) -> dict[str, Any]:
    from email import message_from_bytes, policy

    source = inputs.get("source")
    raw_text = inputs.get("raw")
    if bool(source) == bool(raw_text):
        raise ValueError("cap.eml_parse: exactly one of `source` or `raw` is required")
    store_attachments = bool(inputs.get("store_attachments", True))

    raw = ctx.object_store.get(source) if source else str(raw_text).encode("utf-8")
    if len(raw) > _MAX_SOURCE_BYTES:
        # Guard before message_from_bytes builds the full MIME tree; the
        # attachment-size cap only fires on decoded attachments afterward.
        raise RuntimeError(
            f"cap.eml_parse: raw message is {len(raw)} bytes, exceeding the "
            f"{_MAX_SOURCE_BYTES}-byte limit"
        )

    logger.info(
        "cap.eml_parse start run_id=%s source=%s bytes=%d store_attachments=%s",
        ctx.run_id,
        source or "<inline>",
        len(raw),
        store_attachments,
    )

    # policy.default gives the modern EmailMessage API (get_body,
    # iter_attachments, sane header decoding) instead of the compat32 legacy.
    msg = message_from_bytes(raw, policy=policy.default)

    attachments: list[dict[str, Any]] = []
    total_bytes = 0
    run_prefix = f"runs/{ctx.run_id}/email/{uuid.uuid4().hex}"
    for ordinal, part in enumerate(msg.iter_attachments(), start=1):
        if ordinal > _MAX_ATTACHMENTS:
            raise RuntimeError(
                f"cap.eml_parse: message has more than {_MAX_ATTACHMENTS} "
                f"attachments; refusing"
            )
        decoded = part.get_payload(decode=True)
        # message/rfc822 or multipart sub-parts have no decodable payload;
        # serialize them so nothing is silently dropped.
        payload = decoded if isinstance(decoded, bytes) else part.as_bytes()
        total_bytes += len(payload)
        if total_bytes > _MAX_TOTAL_ATTACHMENT_BYTES:
            raise RuntimeError(
                f"cap.eml_parse: decoded attachments exceed "
                f"{_MAX_TOTAL_ATTACHMENT_BYTES} bytes; refusing"
            )
        filename = _safe_filename(part.get_filename(), ordinal)
        uri: str | None = None
        if store_attachments:
            key = f"{run_prefix}/{ordinal:02d}_{filename}"
            uri = ctx.object_store.put(str(ctx.tenant_id), key, payload).uri
        attachments.append(
            {
                "filename": filename,
                "content_type": part.get_content_type(),
                "size": len(payload),
                "uri": uri,
            }
        )

    out = {
        "subject": str(msg.get("Subject") or ""),
        "from_": str(msg.get("From") or ""),
        "to": _address_list(msg, "To"),
        "cc": _address_list(msg, "Cc"),
        "date": str(msg["Date"]) if msg["Date"] is not None else None,
        "message_id": str(msg["Message-ID"]) if msg["Message-ID"] is not None else None,
        "headers": _all_headers(msg),
        "text": _body_text(msg, "plain"),
        "html": _body_text(msg, "html"),
        "attachments": attachments,
    }

    logger.info(
        "cap.eml_parse ok run_id=%s attachments=%d text=%s html=%s",
        ctx.run_id,
        len(attachments),
        out["text"] is not None,
        out["html"] is not None,
    )
    return out
