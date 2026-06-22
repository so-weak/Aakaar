"""``browser.*`` activity handlers — thin server-side adapters.

The browser primitive *logic* now lives in ``aakaar_caps.caps.browser`` (shared
with a remote agent, so both hosts run byte-identical code). Here we adapt the
server's ``ActivityContext`` to the portable ``CapabilityContext`` and dispatch
to that shared code.

Session bookkeeping helpers (``_SessionHolder`` / ``_stash_key`` /
``_SESSION_PREFIX`` / ``_get_session``) are re-exported for the capability
modules that compose browser primitives, the executor's live-screenshot hook
(keys off the ``browser:`` prefix), and the orchestrator's run-end cleanup.
"""

from __future__ import annotations

import logging
from typing import Any

from aakaar.capabilities._shared import cap_context_from_activity
from aakaar.interpreter.activities.registry import ActivityRegistry
from aakaar.interpreter.activities.types import ActivityContext
from aakaar.interpreter.credentials import fetch_credentials
from aakaar_caps.browser.state import (
    SESSION_PREFIX,
    SessionHolder,
    get_session,
    stash_key,
)
from aakaar_caps.caps import browser as _shared_browser

logger = logging.getLogger(__name__)

# Back-compat names for the rest of the server (capability modules, executor,
# orchestrator). These used to be defined here; they now live in
# aakaar_caps.browser.state and are re-exported under their historical names.
_SESSION_PREFIX = SESSION_PREFIX
_SessionHolder = SessionHolder
_stash_key = stash_key


def _get_session(ctx: ActivityContext, session_id: str) -> Any:
    """Look up a live session stashed by ``browser.open_session``. Accepts the
    server ``ActivityContext`` (reads its ``session_state``) for back-compat
    with the capability modules that import this helper."""
    return get_session(ctx.session_state, session_id)


# ref -> shared run(ctx, inputs)
_RUNS: dict[str, Any] = {spec.ref: run for spec, run in _shared_browser.SPECS}


def _make_handler(ref: str, run: Any) -> Any:
    async def handler(actx: ActivityContext, inputs: dict[str, Any]) -> dict[str, Any]:
        secrets: dict[str, str] = {}
        if ref == "browser.fill_secret":
            # Resolve the vault secret server-side and hand it in via the
            # context; the shared handler reads it from ctx.secrets and never
            # sees the vault. (A PermissionError from a missing grant propagates,
            # matching the historical behavior.)
            secrets = dict(
                fetch_credentials(
                    actx,
                    capability_ref=inputs["capability_ref"],
                    account_alias=inputs["account_alias"],
                )
            )
        ctx = cap_context_from_activity(actx, secrets=secrets)
        return await run(ctx, inputs)

    return handler


# Built once; reused for registration and for the back-compat short-name export.
_HANDLERS: dict[str, Any] = {ref: _make_handler(ref, run) for ref, run in _RUNS.items()}


def register_into(reg: ActivityRegistry) -> None:
    for ref, handler in _HANDLERS.items():
        reg.register(ref, handler)


# Back-compat: the historical module exposed each primitive under its short name
# (e.g. `navigate`, `fill_secret`, `screenshot`) with the (ActivityContext,
# inputs) -> dict signature. Some callers and tests import those directly, so
# re-publish each adapter handler under its short name.
def _export_short_names() -> None:
    g = globals()
    for ref, handler in _HANDLERS.items():
        g[ref.split(".", 1)[1]] = handler


_export_short_names()
