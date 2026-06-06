"""Tests for cap.email_send.

Live SMTP isn't available in unit tests, so we:
  - exercise the pure MIME builder + helpers directly (no I/O), and
  - drive the full async handler with smtplib monkeypatched to a capture
    fake, so attachment fetching from the object store, TLS-mode inference,
    From-address resolution, and the returned recipient list are all
    covered without a server.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest

from aakaar.capabilities.comms.email_send import (
    CAP_REF,
    _clean_addr_list,
    _user_facing_basename,
    build_message,
    definition,
    handler,
)
from aakaar.interpreter.activities.types import ActivityContext
from aakaar.shared.registry import build_default_registry
from aakaar.storage import LocalFsObjectStore
from aakaar.vault import LocalVault

# ---------- pure helpers ---------------------------------------------------


def test_definition_shape() -> None:
    assert definition.ref == CAP_REF == "cap.email_send"
    names = {s.name for s in definition.secrets}
    assert names == {"username", "smtp_password"}


def test_user_facing_basename_strips_uuid_prefix() -> None:
    uri = "aakaar://t/abc/runs/r/" + "f" * 32 + "_report_2026_05.pdf"
    assert _user_facing_basename(uri) == "report_2026_05.pdf"
    assert _user_facing_basename("aakaar://t/abc/stage/plain.csv") == "plain.csv"
    # 32-hex-only (no original) is kept verbatim.
    hexonly = "aakaar://t/abc/x/" + "a" * 32 + ".bin"
    assert _user_facing_basename(hexonly) == "a" * 32 + ".bin"
    assert _user_facing_basename("") == "attachment.bin"


def test_clean_addr_list_trims_dedupes_drops_empty() -> None:
    assert _clean_addr_list([" a@x ", "a@x", "", "b@x", None]) == ["a@x", "b@x"]
    assert _clean_addr_list(None) == []


def test_build_message_plain_only() -> None:
    msg = build_message(
        from_addr="me@x.test",
        to=["a@x.test", "b@x.test"],
        subject="Hi",
        body="hello world",
    )
    assert msg["From"] == "me@x.test"
    assert msg["To"] == "a@x.test, b@x.test"
    assert msg["Subject"] == "Hi"
    assert msg.get_content_type() == "text/plain"
    assert msg.get_content().strip() == "hello world"


def test_build_message_html_alternative_and_cc() -> None:
    msg = build_message(
        from_addr="me@x.test",
        to=["a@x.test"],
        subject="S",
        body="plain",
        html="<p>rich</p>",
        cc=["c@x.test"],
    )
    assert msg["Cc"] == "c@x.test"
    # plain + html -> multipart/alternative with both subtypes present.
    assert msg.get_content_type() == "multipart/alternative"
    subtypes = {p.get_content_subtype() for p in msg.iter_parts()}
    assert {"plain", "html"} <= subtypes


def test_build_message_attachment_uses_filename_and_mime() -> None:
    msg = build_message(
        from_addr="me@x.test",
        to=["a@x.test"],
        subject="S",
        body="body",
        attachments=[("data.csv", b"col1,col2\n1,2\n")],
    )
    atts = list(msg.iter_attachments())
    assert len(atts) == 1
    att = atts[0]
    assert att.get_filename() == "data.csv"
    assert att.get_content_type() == "text/csv"
    # text/* attachments decode to str on read-back.
    assert att.get_content().rstrip("\n") == "col1,col2\n1,2"


# ---------- handler (smtplib monkeypatched) --------------------------------


def _ctx(tmp_path: Path, *, defaults: dict[str, Any], secrets: dict[str, str]) -> ActivityContext:
    tenant_id = uuid.uuid4()
    vault = LocalVault(tmp_path / "vault")
    vault_ref = f"grants/{uuid.uuid4()}"
    vault.put(str(tenant_id), vault_ref, secrets)
    return ActivityContext(
        tenant_id=tenant_id,
        run_id=uuid.uuid4(),
        registry=build_default_registry(),
        object_store=LocalFsObjectStore(tmp_path / "objs"),
        vault=vault,
        granted_capabilities={
            CAP_REF: {"primary": {"vault_ref": vault_ref, "input_defaults": defaults}}
        },
    )


class _FakeSMTP:
    """Captures send_message args; records whether starttls/login ran."""

    instances: list[_FakeSMTP] = []

    def __init__(self, host: str, port: int, timeout: int | None = None) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self.ehlo_count = 0
        self.starttls_called = False
        self.login_args: tuple[str, str] | None = None
        self.sent: dict[str, Any] | None = None
        _FakeSMTP.instances.append(self)

    def __enter__(self) -> _FakeSMTP:
        return self

    def __exit__(self, *a: Any) -> None:
        return None

    def ehlo(self) -> None:
        self.ehlo_count += 1

    def starttls(self, context: Any = None) -> None:
        self.starttls_called = True

    def login(self, user: str, pw: str) -> None:
        self.login_args = (user, pw)

    def send_message(self, msg: Any, from_addr: str, to_addrs: list[str]) -> None:
        self.sent = {"msg": msg, "from_addr": from_addr, "to_addrs": to_addrs}


@pytest.mark.asyncio
async def test_handler_happy_path_starttls_with_attachment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _FakeSMTP.instances.clear()
    import smtplib

    monkeypatch.setattr(smtplib, "SMTP", _FakeSMTP)

    ctx = _ctx(
        tmp_path,
        defaults={"host": "mail.lan.test", "port": 587},
        secrets={"username": "robot@x.test", "smtp_password": "s3cr3t"},
    )
    obj = ctx.object_store.put(
        str(ctx.tenant_id), "stage/" + "b" * 32 + "_invoice.txt", b"PAID\n"
    )

    out = await handler(
        ctx,
        {
            "account_alias": "primary",
            "to": ["dest@x.test", "dest@x.test"],  # dup collapses
            "cc": ["boss@x.test"],
            "subject": "Monthly report",
            "body": "see attached",
            "attachments": [obj.uri],
        },
    )
    assert out == {"sent": True, "to": ["dest@x.test", "boss@x.test"]}

    assert len(_FakeSMTP.instances) == 1
    srv = _FakeSMTP.instances[0]
    assert (srv.host, srv.port) == ("mail.lan.test", 587)
    assert srv.starttls_called is True
    assert srv.login_args == ("robot@x.test", "s3cr3t")
    assert srv.sent is not None
    assert srv.sent["from_addr"] == "robot@x.test"
    assert srv.sent["to_addrs"] == ["dest@x.test", "boss@x.test"]
    # Attachment carried the recovered basename + bytes.
    atts = list(srv.sent["msg"].iter_attachments())
    assert [a.get_filename() for a in atts] == ["invoice.txt"]
    # text/plain attachment decodes to str on read-back.
    assert atts[0].get_content().rstrip("\n") == "PAID"


@pytest.mark.asyncio
async def test_handler_implicit_tls_on_465(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}

    class _FakeSSL(_FakeSMTP):
        def __init__(self, host: str, port: int, timeout: int | None = None,
                     context: Any = None) -> None:
            super().__init__(host, port, timeout)
            captured["used_ssl"] = True

    import smtplib

    monkeypatch.setattr(smtplib, "SMTP_SSL", _FakeSSL)
    # If plain SMTP is touched, the test should fail loudly.
    monkeypatch.setattr(
        smtplib, "SMTP",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("used plain SMTP")),
    )

    ctx = _ctx(
        tmp_path,
        defaults={"host": "smtps.lan.test", "port": 465, "from_addr": "noreply@x.test"},
        secrets={"username": "login", "smtp_password": "pw"},
    )
    out = await handler(
        ctx,
        {"account_alias": "primary", "to": ["x@x.test"], "subject": "s", "body": "b"},
    )
    assert out == {"sent": True, "to": ["x@x.test"]}
    assert captured.get("used_ssl") is True


@pytest.mark.asyncio
async def test_handler_rejects_empty_to(tmp_path: Path) -> None:
    ctx = _ctx(
        tmp_path,
        defaults={"host": "h"},
        secrets={"username": "u@x", "smtp_password": "p"},
    )
    with pytest.raises(ValueError, match="`to` must contain at least one"):
        await handler(ctx, {"account_alias": "primary", "to": ["  "], "body": "b"})


@pytest.mark.asyncio
async def test_handler_rejects_non_managed_attachment(tmp_path: Path) -> None:
    ctx = _ctx(
        tmp_path,
        defaults={"host": "h"},
        secrets={"username": "u@x", "smtp_password": "p"},
    )
    with pytest.raises(ValueError, match="aakaar://"):
        await handler(
            ctx,
            {
                "account_alias": "primary",
                "to": ["a@x.test"],
                "attachments": ["file:///etc/passwd"],
            },
        )


@pytest.mark.asyncio
async def test_handler_requires_host(tmp_path: Path) -> None:
    ctx = _ctx(
        tmp_path,
        defaults={},  # no host
        secrets={"username": "u@x", "smtp_password": "p"},
    )
    with pytest.raises(RuntimeError, match="no SMTP host"):
        await handler(ctx, {"account_alias": "primary", "to": ["a@x.test"]})


@pytest.mark.asyncio
async def test_handler_requires_password(tmp_path: Path) -> None:
    ctx = _ctx(
        tmp_path,
        defaults={"host": "h"},
        secrets={"username": "u@x"},  # no smtp_password
    )
    with pytest.raises(PermissionError, match="smtp_password"):
        await handler(ctx, {"account_alias": "primary", "to": ["a@x.test"]})


@pytest.mark.asyncio
async def test_handler_requires_from_when_username_not_email(tmp_path: Path) -> None:
    ctx = _ctx(
        tmp_path,
        defaults={"host": "h"},  # no from_addr, username has no '@'
        secrets={"username": "robot", "smtp_password": "p"},
    )
    with pytest.raises(RuntimeError, match="no From address"):
        await handler(ctx, {"account_alias": "primary", "to": ["a@x.test"]})
