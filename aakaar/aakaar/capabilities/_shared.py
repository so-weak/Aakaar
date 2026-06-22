"""Register shared-library capabilities into the server.

Capabilities from the `aakaar_caps` package are written once and run on either
host. Here we wrap each into the server's capability shape: a
`CapabilityDefinition` (from its SPEC) plus a handler that builds a server-side
`CapabilityContext` from the run's `ActivityContext` — secrets come from the
vault (via grants), the LLM from the runtime client. They register with a real
local handler, so a node referencing one can run on the SERVER (target=server)
or be dispatched to an agent (target=<agent>) that runs the same code.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aakaar.interpreter.activities.registry import ActivityRegistry
from aakaar.interpreter.activities.types import ActivityContext
from aakaar.interpreter.credentials import fetch_credentials
from aakaar.shared.registry import CapabilityDefinition, Registry, SecretSpec

CapabilityHandler = Callable[
    [ActivityContext, dict[str, Any]], Awaitable[dict[str, Any]]
]

logger = logging.getLogger(__name__)

# Captcha / OTP human prompts time out after this long (matches the historical
# cap.web_login behavior).
_HITL_PROMPT_TIMEOUT_S = 300.0


def cap_context_from_activity(
    actx: ActivityContext, *, secrets: dict[str, str] | None = None
) -> Any:
    """Build a portable ``CapabilityContext`` from the server's
    ``ActivityContext``. This is the single server-side wiring point — both the
    shared ``cap.*`` handlers (via ``register_shared``) and the ``browser.*``
    adapter (``activities/browser.py``) use it, so the code that runs here is
    the same code a remote agent runs against its own runtime + WS proxies.

    ``session_state`` is passed by reference (NOT copied) so a session opened by
    one node is visible to later nodes and to the orchestrator's run-end cleanup.
    Object I/O wraps the (sync) object store in a thread; LLM seams call the
    server's planner client — the agent supplies proxy equivalents instead.
    """
    import aakaar_caps

    store = getattr(actx, "object_store", None)
    tenant = str(actx.tenant_id)

    async def _reader(uri: str) -> bytes:
        if store is None:
            raise aakaar_caps.CapabilityError("object store unavailable")
        return await asyncio.to_thread(store.get, uri)

    async def _writer(key: str, data: bytes) -> str:
        if store is None:
            raise aakaar_caps.CapabilityError("object store unavailable")
        obj = await asyncio.to_thread(store.put, tenant, key, data)
        return obj.uri

    llm = getattr(actx, "llm", None)

    def _text(system: str, user: str) -> str:
        return llm.complete_text(system, user) if llm is not None else ""

    def _plan(messages: Any) -> str:
        # web_login only needs the free-text rationale out of the planner.
        return llm.complete_planner(messages).rationale if llm is not None else ""

    hub = getattr(actx, "signals", None)
    node_id = getattr(actx, "node_id", "") or ""

    async def _signal(message: str, expects: str) -> str:
        # Open a HITL prompt on the server's SignalHub and await the human reply.
        # actx.run_id is a UUID here (the hub keys by it); the captcha timeout
        # matches the historical web_login behavior.
        prompt = await hub.open(
            run_id=actx.run_id, node_id=node_id, message=message, expects=expects
        )
        return await asyncio.wait_for(prompt.future, timeout=_HITL_PROMPT_TIMEOUT_S)

    return aakaar_caps.CapabilityContext(
        secrets=dict(secrets or {}),
        tenant_id=tenant,
        run_id=str(actx.run_id),
        node_id=(node_id or None),
        object_reader=_reader if store is not None else None,
        object_writer=_writer if store is not None else None,
        text_completer=_text if llm is not None else None,
        planner_completer=_plan if llm is not None else None,
        browser_pool=getattr(actx, "browser_pool", None),
        session_state=actx.session_state,
        signals=hub,
        signal_opener=(_signal if (hub is not None and node_id) else None),
        download_mirror_dir=getattr(actx, "download_mirror_dir", None),
    )


def _server_context(actx: ActivityContext, spec: Any, inputs: dict[str, Any]) -> Any:
    secrets: dict[str, str] = {}
    if spec.secrets:
        alias = inputs.get("account_alias")
        if isinstance(alias, str) and alias:
            try:
                secrets = dict(
                    fetch_credentials(actx, capability_ref=spec.ref, account_alias=alias)
                )
            except PermissionError:
                secrets = {}
    return cap_context_from_activity(actx, secrets=secrets)


def definition_from_spec(spec: Any) -> CapabilityDefinition:
    """Build the server CapabilityDefinition from a shared CapabilitySpec —
    single source for both register_shared and the back-compat module shims."""
    return CapabilityDefinition(
        ref=spec.ref,
        description=spec.description,
        input_schema=spec.input_schema,
        output_schema=spec.output_schema,
        side_effecting=spec.side_effecting,
        secrets=tuple(SecretSpec(name=nm, description=ds) for nm, ds in spec.secrets),
        tags=tuple(spec.tags) + (("gui",) if spec.gui else ()),
    )


def server_handler_for(spec: Any, run: Any) -> CapabilityHandler:
    """Wrap a shared cap's run(ctx, inputs) as a server activity handler that
    builds the CapabilityContext from the ActivityContext."""

    async def handler(actx: ActivityContext, inputs: dict[str, Any]) -> dict[str, Any]:
        ctx = _server_context(actx, spec, inputs)
        result: dict[str, Any] = await run(ctx, inputs)
        return result

    return handler


def register_shared(registry: Registry, activities: ActivityRegistry) -> int:
    import aakaar_caps

    n = 0
    for spec, run in aakaar_caps.load_specs():
        if registry.get(spec.ref) is not None:
            continue  # idempotent + avoids a duplicate-ref conflict
        registry.add(definition_from_spec(spec))
        activities.register(spec.ref, server_handler_for(spec, run))
        n += 1
    logger.info("shared capabilities registered into server: %d", n)
    return n


__all__ = ["register_shared"]
