"""Tests for cap.eml_parse.

Builds real RFC822 messages with the stdlib EmailMessage API, round-trips
them through a LocalFsObjectStore-backed ActivityContext, and asserts on
the structured decomposition. Covers:
  - headers / text+html bodies / attachment storage round-trip
  - inline `raw` input and store_attachments=False
  - exactly-one-of source/raw validation (schema + handler)
  - attachment filename sanitization (path-traversal names)
  - attachment-bomb limits (count and total decoded size)
  - definition shape
"""

from __future__ import annotations

import uuid
from email.message import EmailMessage
from pathlib import Path

import pytest
from pydantic import ValidationError

import aakaar.capabilities.comms.eml_parse as eml_parse_mod
from aakaar.capabilities.comms.eml_parse import (
    CAP_REF,
    _safe_filename,
    definition,
    handler,
)
from aakaar.interpreter.activities.types import ActivityContext


def _ctx(tmp_path: Path) -> ActivityContext:
    from aakaar.shared.registry import build_default_registry
    from aakaar.storage import LocalFsObjectStore
    from aakaar.vault import LocalVault

    return ActivityContext(
        tenant_id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        registry=build_default_registry(),
        object_store=LocalFsObjectStore(tmp_path / "objs"),
        vault=LocalVault(tmp_path / "vault"),
    )


def _make_eml(
    *,
    attachments: list[tuple[str, bytes]] | None = None,
    html: str | None = "<p>rich body</p>",
) -> bytes:
    msg = EmailMessage()
    msg["From"] = "Ada Lovelace <ada@sender.test>"
    msg["To"] = "ops@x.test, Grace <grace@x.test>"
    msg["Cc"] = "audit@x.test"
    msg["Subject"] = "Invoice attached"
    msg["Date"] = "Thu, 11 Jun 2026 10:00:00 +0000"
    msg["Message-ID"] = "<msg-1@sender.test>"
    msg["Received"] = "from a.test by b.test"
    msg["Received"] = "from b.test by c.test"
    msg.set_content("plain body here")
    if html:
        msg.add_alternative(html, subtype="html")
    for filename, data in attachments or []:
        msg.add_attachment(
            data,
            maintype="application",
            subtype="octet-stream",
            filename=filename,
        )
    return msg.as_bytes()


def _put_eml(ctx: ActivityContext, key: str, raw: bytes) -> str:
    return ctx.object_store.put(str(ctx.tenant_id), key, raw).uri


# --------------------------------------------------------------------------
# Happy paths
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_parse_stored_eml_full_decomposition(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    uri = _put_eml(
        ctx, "mail/msg.eml", _make_eml(attachments=[("report.bin", b"\x00BYTES")])
    )
    out = await handler(ctx, {"source": uri})

    assert out["subject"] == "Invoice attached"
    assert "ada@sender.test" in out["from_"]
    assert out["to"] == ["ops@x.test", "Grace <grace@x.test>"]
    assert out["cc"] == ["audit@x.test"]
    assert out["date"] == "Thu, 11 Jun 2026 10:00:00 +0000"
    assert out["message_id"] == "<msg-1@sender.test>"
    # Repeated headers are kept, newline-joined, under the lowercase name.
    assert out["headers"]["received"] == "from a.test by b.test\nfrom b.test by c.test"

    assert "plain body here" in (out["text"] or "")
    assert "rich body" in (out["html"] or "")

    assert len(out["attachments"]) == 1
    att = out["attachments"][0]
    assert att["filename"] == "report.bin"
    assert att["content_type"] == "application/octet-stream"
    assert att["size"] == len(b"\x00BYTES")
    assert att["uri"] is not None
    assert ctx.object_store.get(att["uri"]) == b"\x00BYTES"


@pytest.mark.asyncio
async def test_parse_inline_raw(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    out = await handler(
        ctx, {"raw": _make_eml(html=None).decode("utf-8"), "store_attachments": False}
    )
    assert out["subject"] == "Invoice attached"
    assert "plain body here" in (out["text"] or "")
    assert out["html"] is None
    assert out["attachments"] == []


@pytest.mark.asyncio
async def test_store_attachments_false_returns_metadata_only(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    uri = _put_eml(ctx, "m.eml", _make_eml(attachments=[("a.bin", b"12345")]))
    out = await handler(ctx, {"source": uri, "store_attachments": False})
    att = out["attachments"][0]
    assert att["uri"] is None
    assert att["size"] == 5


# --------------------------------------------------------------------------
# Safety properties
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_attachment_filename_traversal_sanitized(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    uri = _put_eml(
        ctx, "m.eml", _make_eml(attachments=[("../../../etc/passwd", b"x")])
    )
    out = await handler(ctx, {"source": uri})
    att = out["attachments"][0]
    assert att["filename"] == "passwd"
    assert ".." not in att["uri"]


@pytest.mark.asyncio
async def test_too_many_attachments_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = _ctx(tmp_path)
    monkeypatch.setattr(eml_parse_mod, "_MAX_ATTACHMENTS", 1)
    uri = _put_eml(ctx, "m.eml", _make_eml(attachments=[("a.bin", b"1"), ("b.bin", b"2")]))
    with pytest.raises(RuntimeError, match="more than 1 attachments"):
        await handler(ctx, {"source": uri})


@pytest.mark.asyncio
async def test_oversized_attachments_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = _ctx(tmp_path)
    monkeypatch.setattr(eml_parse_mod, "_MAX_TOTAL_ATTACHMENT_BYTES", 10)
    uri = _put_eml(ctx, "m.eml", _make_eml(attachments=[("big.bin", b"x" * 100)]))
    with pytest.raises(RuntimeError, match="exceed 10 bytes"):
        await handler(ctx, {"source": uri})


@pytest.mark.asyncio
async def test_oversized_raw_message_refused_before_parse_from_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A huge attachment-free message (giant inline body) must be refused on
    raw size before message_from_bytes builds the MIME tree — the attachment
    caps would never fire on it."""
    ctx = _ctx(tmp_path)
    monkeypatch.setattr(eml_parse_mod, "_MAX_SOURCE_BYTES", 1024)
    msg = EmailMessage()
    msg["From"] = "a@b.test"
    msg["Subject"] = "huge"
    msg.set_content("x" * 4096)  # no attachments at all
    uri = _put_eml(ctx, "m.eml", msg.as_bytes())
    with pytest.raises(RuntimeError, match="exceeding the .*-byte limit"):
        await handler(ctx, {"source": uri})


@pytest.mark.asyncio
async def test_oversized_raw_message_refused_before_parse_inline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same guard binds the inline `raw` path, which also materializes the
    full MIME tree."""
    ctx = _ctx(tmp_path)
    monkeypatch.setattr(eml_parse_mod, "_MAX_SOURCE_BYTES", 64)
    raw = "From: a@b.test\nSubject: huge\n\n" + ("x" * 4096)
    with pytest.raises(RuntimeError, match="exceeding the .*-byte limit"):
        await handler(ctx, {"raw": raw})


# --------------------------------------------------------------------------
# Input validation + definition
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handler_requires_exactly_one_input(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    with pytest.raises(ValueError, match="exactly one"):
        await handler(ctx, {})
    with pytest.raises(ValueError, match="exactly one"):
        await handler(ctx, {"source": "aakaar://t/x/m.eml", "raw": "From: a@b\n\nhi"})


def test_input_schema_requires_exactly_one_input() -> None:
    with pytest.raises(ValidationError):
        definition.input_schema()
    with pytest.raises(ValidationError):
        definition.input_schema(source="aakaar://t/x/m.eml", raw="From: a@b\n\nhi")
    definition.input_schema(raw="From: a@b\n\nhi")  # one is fine


def test_input_schema_forbids_extra_keys() -> None:
    with pytest.raises(ValidationError):
        definition.input_schema(raw="From: a@b\n\nhi", bogus=1)


def test_safe_filename() -> None:
    assert _safe_filename("report.pdf", 1) == "report.pdf"
    assert _safe_filename("dir/inner.txt", 1) == "inner.txt"
    assert _safe_filename("..\\..\\evil.sh", 2) == "evil.sh"
    assert _safe_filename("..", 3) == "attachment-03.bin"
    assert _safe_filename(None, 4) == "attachment-04.bin"


def test_definition_shape() -> None:
    assert definition.ref == CAP_REF == "cap.eml_parse"
    assert definition.secrets == ()
    assert "rfc822" in definition.tags
