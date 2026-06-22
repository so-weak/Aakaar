"""Capability handler registry + dispatch for the agent.

Each handler module exposes ``REF`` (e.g. "cap.shell_exec"), an optional
``VERSION`` ("1") and ``GUI`` (bool), and ``async def run(inputs, secrets) ->
dict``. Modules are discovered at startup; the agent advertises the refs it
loaded. Heavy/OS-specific libraries are imported lazily inside ``run`` so a
headless agent never fails to load a GUI handler module.
"""

from __future__ import annotations

import importlib
import inspect
import logging
import pkgutil
from typing import Any

logger = logging.getLogger(__name__)

_REGISTRY: dict[str, Any] = {}


def load_capabilities() -> dict[str, Any]:
    import aakaar_agent.capabilities as pkg

    for _finder, name, _is_pkg in pkgutil.iter_modules(
        pkg.__path__, prefix="aakaar_agent.capabilities."
    ):
        short = name.rsplit(".", 1)[-1]
        if short.startswith("_"):
            continue
        module = importlib.import_module(name)
        ref = getattr(module, "REF", None)
        run = getattr(module, "run", None)
        if ref and callable(run):
            _REGISTRY[ref] = module
            logger.debug("agent capability loaded ref=%s", ref)

    # Shared-library capabilities (write-once, run server-or-agent). The agent
    # runs the same code as the server with a lightweight context (secrets come
    # from the dispatch envelope; no object store / LLM on the agent).
    try:
        import aakaar_caps

        for spec, run in aakaar_caps.load_specs():
            _REGISTRY[spec.ref] = _SharedCap(spec, run)
            logger.debug("agent shared capability loaded ref=%s", spec.ref)
    except Exception:  # pragma: no cover - shared lib optional
        logger.warning("shared capability library unavailable", exc_info=True)

    logger.info("agent: %d capabilities loaded", len(_REGISTRY))
    return _REGISTRY


class _SharedCap:
    """Adapts a shared-library capability (run(ctx, inputs)) to the agent's
    handler shape (run(inputs, secrets)), building a lightweight context."""

    def __init__(self, spec: Any, run: Any) -> None:
        self.REF = spec.ref
        self.VERSION = str(spec.version)
        self.GUI = bool(spec.gui)
        self._run = run

    async def run(self, inputs: dict[str, Any], secrets: dict[str, str]) -> dict[str, Any]:
        import aakaar_caps

        ctx = aakaar_caps.CapabilityContext(secrets=secrets or {})
        return await self._run(ctx, inputs)

    async def run_with_context(self, ctx: Any, inputs: dict[str, Any]) -> dict[str, Any]:
        """Run against a context the agent runtime built (browser pool, per-run
        session_state, WS-RPC proxies). Used for browser/object-backed caps."""
        return await self._run(ctx, inputs)


def advertised() -> list[dict[str, str]]:
    return [
        {"ref": m.REF, "version": str(getattr(m, "VERSION", "1"))}
        for m in _REGISTRY.values()
    ]


async def dispatch(
    ref: str,
    inputs: dict[str, Any],
    secrets: dict[str, str],
    *,
    context: Any = None,
) -> dict[str, Any]:
    module = _REGISTRY.get(ref)
    if module is None:
        raise KeyError(f"no handler for capability {ref!r}")
    # Shared caps run against the rich CapabilityContext the agent runtime built
    # (browser pool, per-run session_state, WS proxies) when one is provided;
    # desktop caps keep the stateless (inputs, secrets) contract.
    if context is not None and isinstance(module, _SharedCap):
        return await module.run_with_context(context, inputs)
    result = module.run(inputs, secrets)
    if inspect.isawaitable(result):
        result = await result
    return result
