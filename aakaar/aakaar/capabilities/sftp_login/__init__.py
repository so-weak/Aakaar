"""cap.sftp_login — establish an authenticated SFTP-over-SSH session.

Opens an asyncssh connection using credentials from the tenant's vault
(under a `(tenant, cap.sftp_login, account_alias)` grant), starts an
SFTP client on top, and stashes the live handle in `session_state` so
the other `cap.sftp_*` capabilities can reuse it.

Host config lives on the grant's `input_defaults`, not on the DAG:

  host: required. The hostname or IP the SSH server is reachable at.
  port: optional, default 22.
  known_hosts_fingerprint: optional SHA256 host-key fingerprint
    ('SHA256:abc=' or bare base64). Verified after the connection
    handshake; mismatch closes the session and fails the node.
  insecure_skip_host_key_check: optional bool. When true (and no
    fingerprint is set) skips host-key verification entirely. Logged
    at WARNING. Use only for controlled environments.

Credentials in the vault entry:

  username: required.
  password: optional. Used when present and no private_key is set.
  private_key: optional. PEM-encoded private key (Ed25519/RSA/ECDSA).
  private_key_passphrase: optional. Decrypts the private key.

The grant must supply *either* `password` or `private_key`. If both are
present, key auth is tried first and password is offered as fallback
(asyncssh handles the auth-method negotiation).

Returns a `session` handle (string id) the downstream cap.sftp_* nodes
consume. The orchestrator's run-end cleanup closes the connection
automatically if the DAG doesn't explicitly tear it down — there is no
cap.sftp_logout in v1 because the cleanup path is sufficient.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from aakaar.capabilities._sftp_session import (
    SftpSessionHolder,
    normalize_fingerprint,
    stash_key,
)
from aakaar.interpreter.activities.types import ActivityContext
from aakaar.interpreter.credentials import fetch_credentials
from aakaar.shared.registry import CapabilityDefinition, SecretSpec

logger = logging.getLogger(__name__)
CAP_REF = "cap.sftp_login"

_DEFAULT_PORT = 22
_DEFAULT_CONNECT_TIMEOUT_S = 20


class _Inputs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    account_alias: str = Field(
        description="Which credential set to use, e.g. 'primary'. The grant must exist."
    )
    host: str | None = Field(
        default=None,
        description=(
            "SSH hostname or IP. Usually supplied by the grant's input_defaults "
            "— leave null and the executor injects the per-tenant host at run "
            "time. Set explicitly only to override the grant."
        ),
    )
    port: int | None = Field(
        default=None,
        ge=1,
        le=65535,
        description="SSH port. Default 22, overridable via grant input_defaults.",
    )
    connect_timeout_s: int = Field(
        default=_DEFAULT_CONNECT_TIMEOUT_S,
        ge=1,
        le=120,
        description="How long to wait for the TCP+SSH handshake.",
    )


class _Outputs(BaseModel):
    session: str = Field(description="SFTP session handle for downstream cap.sftp_* nodes.")
    host: str = Field(description="Resolved host (for downstream logging/branching).")


definition = CapabilityDefinition(
    ref=CAP_REF,
    description=(
        "Open an authenticated SFTP-over-SSH session using stored credentials "
        "and return a session handle. Host/port live on the grant's "
        "input_defaults; password or private-key auth, depending on what the "
        "grant stores. Optionally verifies the server's host-key fingerprint."
    ),
    input_schema=_Inputs,
    output_schema=_Outputs,
    secrets=(
        SecretSpec(name="username", description="SSH username."),
        SecretSpec(
            name="password",
            description=(
                "SSH password. Optional when a private key is supplied; "
                "stored as empty string if unused."
            ),
        ),
        SecretSpec(
            name="private_key",
            description=(
                "PEM-encoded SSH private key. Optional when a password is "
                "supplied; stored as empty string if unused."
            ),
        ),
        SecretSpec(
            name="private_key_passphrase",
            description="Passphrase decrypting the private key. Optional.",
        ),
    ),
    tags=("auth", "sftp", "ssh"),
)


async def handler(ctx: ActivityContext, inputs: dict[str, Any]) -> dict[str, Any]:
    import asyncssh

    alias = inputs["account_alias"]
    host = inputs.get("host")
    if not host:
        raise RuntimeError(
            f"cap.sftp_login: no host for alias {alias!r}; set `host` on "
            f"the grant's input_defaults"
        )
    port = int(inputs.get("port") or _DEFAULT_PORT)
    connect_timeout = int(inputs.get("connect_timeout_s", _DEFAULT_CONNECT_TIMEOUT_S))

    creds = fetch_credentials(ctx, capability_ref=CAP_REF, account_alias=alias)
    username = (creds.get("username") or "").strip()
    if not username:
        raise PermissionError(
            f"cap.sftp_login: vault entry for alias {alias!r} has no `username`"
        )
    password = creds.get("password") or None
    private_key_pem = creds.get("private_key") or None
    key_passphrase = creds.get("private_key_passphrase") or None

    if not password and not private_key_pem:
        raise PermissionError(
            f"cap.sftp_login: vault entry for alias {alias!r} has neither "
            f"`password` nor `private_key`; one is required"
        )

    # Grant-side host-key policy. We don't pass `known_hosts_fingerprint`
    # straight to asyncssh — we connect with verification skipped, then
    # compare the fingerprint ourselves and tear down on mismatch. That
    # gives us a clear failure message ("fingerprint mismatch") instead
    # of asyncssh's generic auth/handshake error, and it sidesteps the
    # asyncssh known_hosts callable API (which is shaped around files).
    grant_defaults = (
        (ctx.granted_capabilities.get(CAP_REF) or {}).get(alias) or {}
    ).get("input_defaults") or {}
    expected_fp = grant_defaults.get("known_hosts_fingerprint")
    skip_host_key = bool(grant_defaults.get("insecure_skip_host_key_check"))
    if expected_fp:
        known_hosts: Any = None  # verify ourselves below
    elif skip_host_key:
        logger.warning(
            "cap.sftp_login alias=%s host=%s skipping host-key verification "
            "(grant input_defaults set insecure_skip_host_key_check=true)",
            alias,
            host,
        )
        known_hosts = None
    else:
        known_hosts = ()  # asyncssh default: read ~/.ssh/known_hosts

    client_keys: list[Any] = []
    if private_key_pem:
        try:
            client_keys.append(
                asyncssh.import_private_key(private_key_pem, key_passphrase)
            )
        except (asyncssh.KeyImportError, ValueError) as e:
            raise PermissionError(
                f"cap.sftp_login: vault entry for alias {alias!r} has an "
                f"unreadable `private_key`: {e}"
            ) from e

    logger.info(
        "cap.sftp_login start run_id=%s alias=%s host=%s port=%d auth=%s",
        ctx.run_id,
        alias,
        host,
        port,
        "key+password" if (client_keys and password) else ("key" if client_keys else "password"),
    )

    connect_kwargs: dict[str, Any] = {
        "host": host,
        "port": port,
        "username": username,
        "known_hosts": known_hosts,
        "connect_timeout": connect_timeout,
    }
    if client_keys:
        connect_kwargs["client_keys"] = client_keys
    if password:
        connect_kwargs["password"] = password

    conn = await asyncssh.connect(**connect_kwargs)

    # Host-key fingerprint check (when configured). Done post-handshake
    # so the failure cleanly closes the connection rather than leaving
    # an asyncssh task half-set-up.
    if expected_fp:
        try:
            server_key = conn.get_server_host_key()
            got = server_key.get_fingerprint("sha256")
            if normalize_fingerprint(got) != normalize_fingerprint(str(expected_fp)):
                logger.warning(
                    "cap.sftp_login: host key fingerprint mismatch host=%s "
                    "expected=%s got=%s",
                    host,
                    expected_fp,
                    got,
                )
                conn.close()
                await conn.wait_closed()
                raise PermissionError(
                    f"cap.sftp_login: host key fingerprint mismatch for "
                    f"{host}:{port}; expected {expected_fp!r}, got {got!r}"
                )
        except PermissionError:
            raise
        except Exception:
            conn.close()
            await conn.wait_closed()
            raise

    try:
        sftp = await conn.start_sftp_client()
    except Exception:
        conn.close()
        await conn.wait_closed()
        raise

    session_id = uuid.uuid4().hex
    holder = SftpSessionHolder(
        id=session_id, conn=conn, sftp=sftp, host=host, port=port
    )
    ctx.session_state[stash_key(session_id)] = holder
    logger.info(
        "cap.sftp_login ok run_id=%s alias=%s host=%s session=%s",
        ctx.run_id,
        alias,
        host,
        session_id,
    )
    return {"session": session_id, "host": host}
