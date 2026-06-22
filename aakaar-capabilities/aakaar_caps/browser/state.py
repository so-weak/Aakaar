"""Live browser-session bookkeeping shared by the ``browser.*`` capabilities.

A run's first browser primitive (``browser.open_session``) checks a session out
of the pool and stashes a ``_SessionHolder`` in ``ctx.session_state`` keyed by
the session id; later primitives look it up by the id passed in their
``session`` input. The holder keeps the checkout context manager closeable so
the orchestrator's run-end cleanup can release a leaked session.

This lives in ``aakaar_caps`` so the SAME bookkeeping runs on the server and on
a remote agent. The server re-exports ``_SessionHolder`` / ``_stash_key`` /
``_SESSION_PREFIX`` from ``aakaar.interpreter.activities.browser`` for the
executor's live-screenshot hook and the orchestrator's cleanup loop.
"""

from __future__ import annotations

import contextlib
from typing import Any, cast

from aakaar_caps.browser.session import BrowserSession

SESSION_PREFIX = "browser:"


def stash_key(session_id: str) -> str:
    return f"{SESSION_PREFIX}{session_id}"


def get_session(session_state: dict[str, Any], session_id: str) -> BrowserSession:
    holder = cast("SessionHolder | None", session_state.get(stash_key(session_id)))
    if holder is None:
        raise RuntimeError(
            f"no live browser session for id {session_id!r}. Open one upstream with "
            "browser.open_session / cap.web_login / cap.open_url. Note: a browser "
            "session is NOT durable — it does not survive a server restart, an agent "
            "reconnect, or being routed to a different agent, so a run cannot be "
            "resumed past the node that opened it."
        )
    return holder.session


class SessionHolder:
    """Lives in ``session_state``. Keeps the session reachable by primitives
    (``.session``) and the underlying checkout context manager closeable by the
    run-end cleanup (``.close()``)."""

    def __init__(self, cm: Any, session: BrowserSession) -> None:
        self._cm = cm
        self.session = session
        self._closed = False

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        with contextlib.suppress(Exception):
            await self._cm.__aexit__(None, None, None)
