"""Tests for cap.sftp_login.

Drives the handler directly with `asyncssh.connect` monkeypatched to
return a `FakeSshConn`, so the tests exercise:
  - credential resolution from vault (PermissionError shapes)
  - host-key fingerprint policy (match / mismatch / skip / unset)
  - auth-method selection (password / key / key+password)
  - cleanup invariants (connection torn down on auth failure or SFTP
    handshake failure)
  - the resulting `SftpSessionHolder` shows up in session_state with
    the expected `host`/`port`.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import asyncssh
import pytest

from aakaar.capabilities._sftp_session import SftpSessionHolder, stash_key
from aakaar.capabilities.sftp_login import CAP_REF, handler
from aakaar.interpreter.activities.types import ActivityContext
from aakaar.shared.registry import build_default_registry
from aakaar.storage import LocalFsObjectStore
from aakaar.vault import LocalVault
from tests._sftp_fakes import FakeSftpClient, FakeSshConn


@pytest.fixture()
def _patch_connect(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Install a recording substitute for asyncssh.connect / asyncssh.import_private_key.

    Returns a dict the caller mutates to control behavior:
      - `conn`: the FakeSshConn the patched connect() returns
      - `connect_kwargs`: list of kwargs each call received
      - `connect_error`: optional Exception for connect() to raise
      - `key_objects`: list returned for each import_private_key call
      - `key_import_error`: optional Exception for import_private_key
    """
    state: dict[str, Any] = {
        "conn": FakeSshConn(sftp=FakeSftpClient()),
        "connect_kwargs": [],
        "connect_error": None,
        "key_objects": [],
        "key_import_error": None,
    }

    async def fake_connect(**kwargs: Any) -> FakeSshConn:
        state["connect_kwargs"].append(kwargs)
        if state["connect_error"] is not None:
            raise state["connect_error"]
        return state["conn"]

    def fake_import_key(pem: str, passphrase: str | None = None) -> object:  # noqa: ARG001
        if state["key_import_error"] is not None:
            raise state["key_import_error"]
        sentinel = object()
        state["key_objects"].append(sentinel)
        return sentinel

    monkeypatch.setattr(asyncssh, "connect", fake_connect)
    monkeypatch.setattr(asyncssh, "import_private_key", fake_import_key)
    return state


def _ctx(
    tmp_path: Path,
    *,
    creds: dict[str, str],
    input_defaults: dict[str, Any] | None = None,
) -> tuple[ActivityContext, str]:
    """Seed a vault entry, build an ActivityContext with a granted alias
    pointed at it, and return (ctx, alias)."""
    tenant_id = uuid.uuid4()
    vault = LocalVault(tmp_path / "vault")
    vault_ref = f"grants/{uuid.uuid4()}"
    vault.put(str(tenant_id), vault_ref, creds)
    ctx = ActivityContext(
        tenant_id=tenant_id,
        run_id=uuid.uuid4(),
        registry=build_default_registry(),
        object_store=LocalFsObjectStore(tmp_path / "objs"),
        vault=vault,
        granted_capabilities={
            CAP_REF: {
                "primary": {
                    "vault_ref": vault_ref,
                    "input_defaults": input_defaults or {},
                }
            }
        },
    )
    return ctx, "primary"


@pytest.mark.asyncio
async def test_password_auth_stashes_session(
    tmp_path: Path, _patch_connect: dict[str, Any]
) -> None:
    ctx, alias = _ctx(tmp_path, creds={"username": "ops", "password": "s3cret"})
    out = await handler(
        ctx,
        {"account_alias": alias, "host": "sftp.example.test", "port": 2222},
    )

    assert out["host"] == "sftp.example.test"
    session_id = out["session"]
    holder = ctx.session_state[stash_key(session_id)]
    assert isinstance(holder, SftpSessionHolder)
    assert holder.host == "sftp.example.test"
    assert holder.port == 2222

    # connect() was called with the right shape.
    kwargs = _patch_connect["connect_kwargs"][0]
    assert kwargs["host"] == "sftp.example.test"
    assert kwargs["port"] == 2222
    assert kwargs["username"] == "ops"
    assert kwargs["password"] == "s3cret"
    assert "client_keys" not in kwargs  # password-only
    # No fingerprint and no skip flag → asyncssh default (empty tuple).
    assert kwargs["known_hosts"] == ()


@pytest.mark.asyncio
async def test_private_key_auth_imports_pem(
    tmp_path: Path, _patch_connect: dict[str, Any]
) -> None:
    ctx, alias = _ctx(
        tmp_path,
        creds={
            "username": "ops",
            "password": "",
            "private_key": "-----BEGIN OPENSSH PRIVATE KEY-----\nblah\n-----END OPENSSH PRIVATE KEY-----",
            "private_key_passphrase": "pp",
        },
    )
    out = await handler(ctx, {"account_alias": alias, "host": "h"})
    assert out["session"]

    kwargs = _patch_connect["connect_kwargs"][0]
    assert "password" not in kwargs
    assert len(kwargs["client_keys"]) == 1
    assert kwargs["client_keys"][0] is _patch_connect["key_objects"][0]


@pytest.mark.asyncio
async def test_key_plus_password_both_offered(
    tmp_path: Path, _patch_connect: dict[str, Any]
) -> None:
    """When the grant carries both, the handler hands both to asyncssh
    and lets the server pick — server policy decides the order."""
    ctx, alias = _ctx(
        tmp_path,
        creds={"username": "u", "password": "pw", "private_key": "k"},
    )
    await handler(ctx, {"account_alias": alias, "host": "h"})
    kwargs = _patch_connect["connect_kwargs"][0]
    assert kwargs["password"] == "pw"
    assert len(kwargs["client_keys"]) == 1


@pytest.mark.asyncio
async def test_missing_host_fails_loudly(
    tmp_path: Path, _patch_connect: dict[str, Any]
) -> None:
    ctx, alias = _ctx(tmp_path, creds={"username": "u", "password": "p"})
    with pytest.raises(RuntimeError, match="no host"):
        await handler(ctx, {"account_alias": alias})
    # connect must not have been called when the host is missing.
    assert _patch_connect["connect_kwargs"] == []


@pytest.mark.asyncio
async def test_no_credentials_at_all_is_permission_error(
    tmp_path: Path, _patch_connect: dict[str, Any]
) -> None:
    ctx, alias = _ctx(
        tmp_path, creds={"username": "u", "password": "", "private_key": ""}
    )
    with pytest.raises(PermissionError, match="neither.*password.*nor.*private_key"):
        await handler(ctx, {"account_alias": alias, "host": "h"})
    assert _patch_connect["connect_kwargs"] == []


@pytest.mark.asyncio
async def test_missing_username_is_permission_error(
    tmp_path: Path, _patch_connect: dict[str, Any]
) -> None:
    ctx, alias = _ctx(tmp_path, creds={"username": "  ", "password": "p"})
    with pytest.raises(PermissionError, match="no `username`"):
        await handler(ctx, {"account_alias": alias, "host": "h"})


@pytest.mark.asyncio
async def test_bad_private_key_is_permission_error(
    tmp_path: Path, _patch_connect: dict[str, Any]
) -> None:
    _patch_connect["key_import_error"] = asyncssh.KeyImportError("garbage")
    ctx, alias = _ctx(
        tmp_path, creds={"username": "u", "password": "", "private_key": "junk"}
    )
    with pytest.raises(PermissionError, match="unreadable `private_key`"):
        await handler(ctx, {"account_alias": alias, "host": "h"})


@pytest.mark.asyncio
async def test_fingerprint_match_succeeds(
    tmp_path: Path, _patch_connect: dict[str, Any]
) -> None:
    _patch_connect["conn"] = FakeSshConn(
        sftp=FakeSftpClient(), server_fingerprint="SHA256:abcdef="
    )
    ctx, alias = _ctx(
        tmp_path,
        creds={"username": "u", "password": "p"},
        input_defaults={"known_hosts_fingerprint": "abcdef="},  # normalized equal
    )
    out = await handler(ctx, {"account_alias": alias, "host": "h"})
    assert out["session"]
    # When verifying ourselves we pass known_hosts=None to asyncssh.
    assert _patch_connect["connect_kwargs"][0]["known_hosts"] is None


@pytest.mark.asyncio
async def test_fingerprint_mismatch_closes_connection_and_raises(
    tmp_path: Path, _patch_connect: dict[str, Any]
) -> None:
    conn = FakeSshConn(sftp=FakeSftpClient(), server_fingerprint="SHA256:xxx")
    _patch_connect["conn"] = conn
    ctx, alias = _ctx(
        tmp_path,
        creds={"username": "u", "password": "p"},
        input_defaults={"known_hosts_fingerprint": "SHA256:yyy"},
    )
    with pytest.raises(PermissionError, match="fingerprint mismatch"):
        await handler(ctx, {"account_alias": alias, "host": "h"})

    assert conn.closed is True
    assert conn.waited_closed is True
    # No SFTP client should have been started.
    assert ctx.session_state == {}


@pytest.mark.asyncio
async def test_insecure_skip_passes_none(
    tmp_path: Path, _patch_connect: dict[str, Any]
) -> None:
    ctx, alias = _ctx(
        tmp_path,
        creds={"username": "u", "password": "p"},
        input_defaults={"insecure_skip_host_key_check": True},
    )
    await handler(ctx, {"account_alias": alias, "host": "h"})
    assert _patch_connect["connect_kwargs"][0]["known_hosts"] is None


@pytest.mark.asyncio
async def test_start_sftp_client_failure_closes_connection(
    tmp_path: Path, _patch_connect: dict[str, Any]
) -> None:
    conn = FakeSshConn(
        sftp=FakeSftpClient(), start_sftp_error=RuntimeError("subsystem refused")
    )
    _patch_connect["conn"] = conn
    ctx, alias = _ctx(tmp_path, creds={"username": "u", "password": "p"})
    with pytest.raises(RuntimeError, match="subsystem refused"):
        await handler(ctx, {"account_alias": alias, "host": "h"})
    assert conn.closed is True
    assert ctx.session_state == {}


@pytest.mark.asyncio
async def test_default_port_used_when_unspecified(
    tmp_path: Path, _patch_connect: dict[str, Any]
) -> None:
    ctx, alias = _ctx(tmp_path, creds={"username": "u", "password": "p"})
    await handler(ctx, {"account_alias": alias, "host": "h"})
    assert _patch_connect["connect_kwargs"][0]["port"] == 22


@pytest.mark.asyncio
async def test_close_is_idempotent_and_closes_underlying(
    tmp_path: Path, _patch_connect: dict[str, Any]
) -> None:
    """Orchestrator-style cleanup calls `.close()` on every session_state
    entry; calling it a second time must not throw."""
    ctx, alias = _ctx(tmp_path, creds={"username": "u", "password": "p"})
    out = await handler(ctx, {"account_alias": alias, "host": "h"})
    holder: SftpSessionHolder = ctx.session_state[stash_key(out["session"])]

    await holder.close()
    assert _patch_connect["conn"].closed is True
    assert _patch_connect["conn"].sftp is not None
    assert _patch_connect["conn"].sftp.exited is True

    # Second close is a no-op — no exception, no second teardown.
    _patch_connect["conn"].closed = False
    _patch_connect["conn"].sftp.exited = False
    await holder.close()
    assert _patch_connect["conn"].closed is False
    assert _patch_connect["conn"].sftp.exited is False
