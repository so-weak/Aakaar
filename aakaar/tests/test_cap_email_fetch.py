"""Tests for cap.email_fetch.

A live IMAP server is out of scope for unit tests, so we exercise:
  - input schema validation (mailbox default, since shape, limit bounds)
  - the pure header/body/search helpers against hand-built email bytes
  - the handler's credential + host resolution and SEARCH/limit/newest-first
    logic by stubbing the blocking IMAP session (`_fetch_sync`).
The live network path is covered only indirectly; nothing here opens a
socket.
"""

from __future__ import annotations

import uuid
from email.message import EmailMessage
from pathlib import Path

import pytest

from aakaar.capabilities.comms.email_fetch import (
    CAP_REF,
    _build_search_criteria,
    _decode_header,
    _extract_body_preview,
    _first_rfc822_payload,
    _imap_date,
    _Inputs,
    _summarize_message,
    definition,
    handler,
)
from aakaar.interpreter.activities.types import ActivityContext

# ---------- ctx helper -----------------------------------------------------


def _make_ctx(tmp_path: Path, *, granted: dict | None = None) -> ActivityContext:
    from aakaar.shared.registry import build_default_registry
    from aakaar.storage import LocalFsObjectStore
    from aakaar.vault import LocalVault

    return ActivityContext(
        tenant_id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        registry=build_default_registry(),
        object_store=LocalFsObjectStore(tmp_path / "objs"),
        vault=LocalVault(tmp_path / "vault"),
        granted_capabilities=granted or {},
    )


def _grant_ctx(tmp_path: Path, secrets: dict, *, input_defaults: dict | None = None):
    ctx = _make_ctx(tmp_path)
    vault_ref = f"grants/{uuid.uuid4()}"
    ctx.vault.put(str(ctx.tenant_id), vault_ref, secrets)
    ctx.granted_capabilities = {
        CAP_REF: {
            "primary": {"vault_ref": vault_ref, "input_defaults": input_defaults or {}}
        }
    }
    return ctx


# ---------- definition -----------------------------------------------------


def test_definition_shape() -> None:
    assert definition.ref == CAP_REF == "cap.email_fetch"
    secret_names = {s.name for s in definition.secrets}
    assert {"username", "imap_password"} <= secret_names


def test_capability_autoloads_into_registry() -> None:
    from aakaar.capabilities import load_into
    from aakaar.interpreter import build_default_activities
    from aakaar.shared.registry import build_default_registry

    registry = build_default_registry()
    load_into(registry, build_default_activities())
    assert registry.get(CAP_REF) is not None


# ---------- input validation -----------------------------------------------


def test_inputs_defaults() -> None:
    i = _Inputs(account_alias="primary")
    assert i.mailbox == "INBOX"
    assert i.limit == 20
    assert i.since is None


def test_inputs_reject_unknown_field() -> None:
    with pytest.raises(ValueError):
        _Inputs(account_alias="primary", folder="INBOX")  # type: ignore[call-arg]


def test_inputs_reject_bad_since() -> None:
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        _Inputs(account_alias="primary", since="06/03/2026")


def test_inputs_blank_since_becomes_none() -> None:
    assert _Inputs(account_alias="primary", since="   ").since is None


@pytest.mark.parametrize("bad", [0, 201, -5])
def test_inputs_limit_bounds(bad: int) -> None:
    with pytest.raises(ValueError):
        _Inputs(account_alias="primary", limit=bad)


# ---------- pure helpers ---------------------------------------------------


def test_imap_date_formats() -> None:
    assert _imap_date("2026-01-02") == "02-Jan-2026"


def test_build_search_criteria_all_when_empty() -> None:
    assert _build_search_criteria(
        subject_contains=None, from_contains=None, since=None
    ) == ["ALL"]


def test_build_search_criteria_combines_filters() -> None:
    crit = _build_search_criteria(
        subject_contains="invoice",
        from_contains="bank@x.test",
        since="2026-01-02",
    )
    assert crit == [
        "SINCE",
        "02-Jan-2026",
        "SUBJECT",
        "invoice",
        "FROM",
        "bank@x.test",
    ]


def test_decode_header_rfc2047() -> None:
    # "Faktura" encoded as a UTF-8 encoded-word.
    raw = "=?utf-8?q?Fakt=C3=B8ra?="
    assert _decode_header(raw) == "Fakt-r-a".replace("-r-", "ør")  # Faktøra


def test_decode_header_empty_and_plain() -> None:
    assert _decode_header(None) == ""
    assert _decode_header("Plain Subject") == "Plain Subject"


def _plain_msg(subject: str, frm: str, body: str) -> bytes:
    m = EmailMessage()
    m["Subject"] = subject
    m["From"] = frm
    m["Date"] = "Tue, 02 Jun 2026 10:00:00 +0000"
    m.set_content(body)
    return m.as_bytes()


def test_extract_body_preview_plain() -> None:
    raw = _plain_msg("S", "a@b.test", "Hello   there\n\nworld\n")
    from email import message_from_bytes
    from email.policy import default as default_policy

    msg = message_from_bytes(raw, policy=default_policy)
    assert _extract_body_preview(msg) == "Hello there world"


def test_extract_body_preview_html_fallback() -> None:
    m = EmailMessage()
    m["Subject"] = "html only"
    m.set_content("<p>Hi&amp;bye <b>now</b></p>", subtype="html")
    from email import message_from_bytes
    from email.policy import default as default_policy

    msg = message_from_bytes(m.as_bytes(), policy=default_policy)
    assert _extract_body_preview(msg) == "Hi&bye now"


def test_extract_body_preview_truncates() -> None:
    raw = _plain_msg("S", "a@b.test", "x" * 1000)
    from email import message_from_bytes
    from email.policy import default as default_policy

    msg = message_from_bytes(raw, policy=default_policy)
    assert len(_extract_body_preview(msg)) == 500


def test_summarize_message() -> None:
    raw = _plain_msg(
        "=?utf-8?q?Invoice_#42?=", "Bank <bank@x.test>", "Pay now please"
    )
    out = _summarize_message("17", raw)
    assert out["uid"] == "17"
    assert out["subject"] == "Invoice #42"
    assert out["from_"] == "Bank <bank@x.test>"
    assert "02 Jun 2026" in out["date"]
    assert out["body_preview"] == "Pay now please"


def test_first_rfc822_payload_picks_body_tuple() -> None:
    fetched = [
        (b"17 (BODY[] {12}", b"RAW-BODY-XYZ"),
        b")",
    ]
    assert _first_rfc822_payload(fetched) == b"RAW-BODY-XYZ"


def test_first_rfc822_payload_none_when_no_body() -> None:
    assert _first_rfc822_payload([b")"]) is None


# ---------- handler: credential + host resolution (IMAP stubbed) -----------


@pytest.mark.asyncio
async def test_handler_missing_grant_raises(tmp_path: Path) -> None:
    ctx = _make_ctx(tmp_path)
    with pytest.raises(PermissionError, match="no grant"):
        await handler(ctx, {"account_alias": "primary"})


@pytest.mark.asyncio
async def test_handler_missing_password_raises(tmp_path: Path) -> None:
    ctx = _grant_ctx(tmp_path, {"username": "u", "host": "imap.x.test"})
    with pytest.raises(PermissionError, match="imap_password"):
        await handler(ctx, {"account_alias": "primary"})


@pytest.mark.asyncio
async def test_handler_missing_host_raises(tmp_path: Path) -> None:
    ctx = _grant_ctx(tmp_path, {"username": "u", "imap_password": "p"})
    with pytest.raises(RuntimeError, match="no IMAP host"):
        await handler(ctx, {"account_alias": "primary"})


@pytest.mark.asyncio
async def test_handler_resolves_host_port_and_criteria(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Host falls back to grant input_defaults; the handler hands the right
    host/port/criteria/limit to the blocking IMAP worker and returns its
    messages verbatim. The real IMAP session is replaced wholesale."""
    ctx = _grant_ctx(
        tmp_path,
        {"username": "u", "imap_password": "p"},
        input_defaults={"host": "imap.bank.test", "port": 1993},
    )

    captured: dict = {}

    def _fake_fetch_sync(**kwargs):
        captured.update(kwargs)
        return [
            {
                "uid": "5",
                "subject": "Statement",
                "from_": "bank@x.test",
                "date": "today",
                "body_preview": "hi",
            }
        ]

    import aakaar.capabilities.comms.email_fetch as mod

    monkeypatch.setattr(mod, "_fetch_sync", _fake_fetch_sync)

    out = await handler(
        ctx,
        {
            "account_alias": "primary",
            "mailbox": "Archive",
            "subject_contains": "Statement",
            "since": "2026-01-02",
            "limit": 5,
        },
    )

    assert out == {
        "messages": [
            {
                "uid": "5",
                "subject": "Statement",
                "from_": "bank@x.test",
                "date": "today",
                "body_preview": "hi",
            }
        ]
    }
    assert captured["host"] == "imap.bank.test"
    assert captured["port"] == 1993
    assert captured["username"] == "u"
    assert captured["mailbox"] == "Archive"
    assert captured["limit"] == 5
    assert captured["criteria"] == ["SINCE", "02-Jan-2026", "SUBJECT", "Statement"]


@pytest.mark.asyncio
async def test_handler_vault_host_overrides_grant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = _grant_ctx(
        tmp_path,
        {"username": "u", "imap_password": "p", "host": "vault-host.test"},
        input_defaults={"host": "grant-host.test"},
    )
    captured: dict = {}

    def _fake_fetch_sync(**kwargs):
        captured.update(kwargs)
        return []

    import aakaar.capabilities.comms.email_fetch as mod

    monkeypatch.setattr(mod, "_fetch_sync", _fake_fetch_sync)
    await handler(ctx, {"account_alias": "primary"})
    assert captured["host"] == "vault-host.test"
    assert captured["port"] == 993  # default when unset
