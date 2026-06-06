"""Shared session handle for the cap.sftp_* capabilities.

A successful `cap.sftp_login` stashes an `SftpSessionHolder` in
`ActivityContext.session_state` under the key `sftp:<session_id>`. The
downstream cap.sftp_* nodes look it up by handle and use the live
`asyncssh.SFTPClient` to do their work.

The orchestrator's run-end cleanup calls `.close()` on every entry in
`session_state` (see `aakaar/interpreter/orchestrator.py`), so this holder
exposes an idempotent async `close()` that tears down the SFTP client
and the underlying SSH connection.

Helpers here are deliberately small — host-key resolution and credential
selection live with the login capability; this module is just a typed
stash + path/host-key utilities so the other four capabilities don't
have to know the layout.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from aakaar.interpreter.activities.types import ActivityContext

logger = logging.getLogger(__name__)

SFTP_STASH_PREFIX = "sftp:"


if TYPE_CHECKING:
    import asyncssh


def stash_key(session_id: str) -> str:
    return f"{SFTP_STASH_PREFIX}{session_id}"


@dataclass
class SftpSessionHolder:
    """Lives in `ActivityContext.session_state`. Carries the live SSH +
    SFTP clients and the host the session is pinned to (so the
    capabilities can log meaningfully on failure without inspecting
    private asyncssh attrs)."""

    id: str
    conn: asyncssh.SSHClientConnection
    sftp: asyncssh.SFTPClient
    host: str
    port: int

    _closed: bool = False

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self.sftp.exit()
        except Exception:  # noqa: BLE001
            logger.debug("sftp client exit raised", exc_info=True)
        try:
            self.conn.close()
            await self.conn.wait_closed()
        except Exception:  # noqa: BLE001
            logger.debug("ssh connection close raised", exc_info=True)


def get_holder(ctx: ActivityContext, session_id: str) -> SftpSessionHolder:
    holder = ctx.session_state.get(stash_key(session_id))
    if holder is None:
        raise RuntimeError(
            f"no live sftp session for id {session_id!r}; was cap.sftp_login called?"
        )
    if not isinstance(holder, SftpSessionHolder):
        raise RuntimeError(
            f"session_state entry for {session_id!r} is not an SFTP holder "
            f"(got {type(holder).__name__}); session-id collision?"
        )
    return holder


def normalize_remote_path(p: str) -> str:
    """Reject empty paths; leave everything else alone.

    We deliberately don't try to sandbox the remote side — the SFTP
    server enforces its own ACLs and chroot. What we *do* enforce is
    that the path is non-empty and a string, so the planner can't pass
    `None`/`""` and accidentally list the connection root.
    """
    if not isinstance(p, str) or not p.strip():
        raise ValueError("remote path must be a non-empty string")
    return p


def normalize_fingerprint(fp: str) -> str:
    """Strip a leading 'SHA256:' prefix and surrounding whitespace so two
    equivalent forms ('SHA256:abc=' and 'abc=') compare equal."""
    s = fp.strip()
    if s.startswith("SHA256:"):
        s = s[len("SHA256:") :]
    return s
