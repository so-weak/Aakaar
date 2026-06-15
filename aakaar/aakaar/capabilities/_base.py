"""Capability authoring base + loader.

A capability module exposes:
  - `definition: CapabilityDefinition` — registered with the registry
  - `handler: CapabilityHandler` — registered with the activity registry

Capabilities have access to the same `ActivityContext` as actions, plus a
helper for fetching the capability's own credentials from the vault. The
helper enforces:
  - the requested account_alias must exist in the tenant's grants for this
    capability (the planner has already gated visibility, but defense in
    depth)
  - secrets are fetched fresh per-call and never returned to the DAG env

Capabilities are loaded once at startup from the `aakaar.capabilities`
package (excluding modules whose names start with `_`). Hot-reload is not
a v1 feature.
"""

from __future__ import annotations

import importlib
import logging
import pkgutil
from collections.abc import Awaitable, Callable
from typing import Any

from aakaar.interpreter.activities.registry import ActivityRegistry
from aakaar.interpreter.activities.types import ActivityContext
from aakaar.interpreter.credentials import fetch_credentials as _fetch_credentials
from aakaar.shared.registry import CapabilityDefinition, Registry

logger = logging.getLogger(__name__)


CapabilityHandler = Callable[[ActivityContext, dict[str, Any]], Awaitable[dict[str, Any]]]


class CapabilityModule:
    """Marker — every capability module is expected to define `definition`
    and `handler` at module top-level. We don't enforce a base class to
    keep capability authoring lightweight."""

    definition: CapabilityDefinition
    handler: CapabilityHandler


# Re-export so existing capability modules keep working.
fetch_credentials = _fetch_credentials


def load_into(registry: Registry, activities: ActivityRegistry, *, package: str = __package__) -> int:
    """Discover all capability modules under `package` and register them.

    Returns the number of capabilities loaded.
    """
    parent_pkg = package.rsplit(".", 1)[0] if package and "._base" in package else package
    pkg = importlib.import_module(parent_pkg)
    n = 0

    # walk_packages recurses into sub-packages so capabilities can be grouped
    # (e.g. capabilities/web/web_scrape/). Grouping packages and helper modules
    # (names starting with "_") don't expose definition/handler and are skipped.
    def _on_error(name: str) -> None:
        logger.warning("capability loader: failed to import %s", name)

    for _finder, name, is_pkg in pkgutil.walk_packages(
        pkg.__path__, prefix=f"{parent_pkg}.", onerror=_on_error
    ):
        short = name.rsplit(".", 1)[-1]
        if short.startswith("_"):
            continue
        module = importlib.import_module(name)
        definition = getattr(module, "definition", None)
        handler: CapabilityHandler | None = getattr(module, "handler", None)
        remote_only = bool(getattr(module, "remote_only", False))
        if definition is None or (handler is None and not remote_only):
            # Either a grouping sub-package or a capability's private helper
            # submodule (e.g. web_login.discovery). Neither is a capability;
            # skip quietly. A genuinely broken capability surfaces as a missing
            # ref at plan/run time.
            logger.debug(
                "capability loader: %s has no definition/handler; skipped (is_pkg=%s)",
                name,
                is_pkg,
            )
            continue
        registry.add(definition)
        if remote_only:
            # Contract-only: the schema/tags live here for validation, planning,
            # and placement, but the implementation runs on a remote agent — no
            # local activity handler is registered.
            logger.debug("remote capability registered (no local handler) ref=%s module=%s", definition.ref, name)
        else:
            # The guard above (`handler is None and not remote_only`) already
            # `continue`d if a non-remote capability had no handler; assert it
            # so mypy narrows the Optional for the registry call.
            assert handler is not None
            activities.register(definition.ref, handler)
            logger.debug("capability registered ref=%s module=%s", definition.ref, name)
        n += 1

    # Shared-library capabilities (write-once, run server-or-agent) register with
    # real local handlers, so they can run on the server or be dispatched to an
    # agent. Failures here must not break local capability loading.
    try:
        from aakaar.capabilities._shared import register_shared

        n += register_shared(registry, activities)
    except Exception:  # pragma: no cover - shared lib optional/absent
        logger.warning("shared capability registration skipped", exc_info=True)
    logger.info("capability loader: %d capabilities loaded from %s", n, parent_pkg)
    return n
