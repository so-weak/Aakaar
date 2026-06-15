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


def _server_context(actx: ActivityContext, spec: Any, inputs: dict[str, Any]) -> Any:
    import aakaar_caps

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
    llm = getattr(actx, "llm", None)

    def completer(system: str, user: str) -> str:
        return llm.complete_text(system, user) if llm is not None else ""

    return aakaar_caps.CapabilityContext(
        secrets=secrets,
        tenant_id=str(actx.tenant_id),
        run_id=str(actx.run_id),
        text_completer=completer if llm is not None else None,
        # object_reader/writer intentionally None for now: the migrated caps are
        # pure compute. Object-backed shared caps would wire these to
        # actx.object_store when added.
    )


def register_shared(registry: Registry, activities: ActivityRegistry) -> int:
    import aakaar_caps

    n = 0
    for spec, run in aakaar_caps.load_specs():
        if registry.get(spec.ref) is not None:
            continue  # idempotent + avoids a duplicate-ref conflict
        defn = CapabilityDefinition(
            ref=spec.ref,
            description=spec.description,
            input_schema=spec.input_schema,
            output_schema=spec.output_schema,
            secrets=tuple(SecretSpec(name=nm, description=ds) for nm, ds in spec.secrets),
            tags=tuple(spec.tags) + (("gui",) if spec.gui else ()),
        )

        def make_handler(spec: Any = spec, run: Any = run) -> CapabilityHandler:
            async def handler(actx: ActivityContext, inputs: dict[str, Any]) -> dict[str, Any]:
                ctx = _server_context(actx, spec, inputs)
                result: dict[str, Any] = await run(ctx, inputs)
                return result

            return handler

        registry.add(defn)
        activities.register(spec.ref, make_handler())
        n += 1
    logger.info("shared capabilities registered into server: %d", n)
    return n


__all__ = ["register_shared"]
