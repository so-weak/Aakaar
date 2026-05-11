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

Capabilities are loaded once at startup from the `aakar.capabilities`
package (excluding modules whose names start with `_`). Hot-reload is not
a v1 feature.
"""

from __future__ import annotations

import importlib
import logging
import pkgutil
from collections.abc import Awaitable, Callable
from typing import Any

from aakar.interpreter.activities.registry import ActivityRegistry
from aakar.interpreter.activities.types import ActivityContext
from aakar.interpreter.credentials import fetch_credentials as _fetch_credentials
from aakar.shared.registry import CapabilityDefinition, Registry


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
    for finder, name, _is_pkg in pkgutil.iter_modules(pkg.__path__, prefix=f"{parent_pkg}."):
        short = name.rsplit(".", 1)[-1]
        if short.startswith("_"):
            continue
        module = importlib.import_module(name)
        definition = getattr(module, "definition", None)
        handler = getattr(module, "handler", None)
        if definition is None or handler is None:
            logger.warning("capability module %s missing definition/handler; skipped", name)
            continue
        registry.add(definition)
        activities.register(definition.ref, handler)
        logger.debug("capability registered ref=%s module=%s", definition.ref, name)
        n += 1
    logger.info("capability loader: %d capabilities loaded from %s", n, parent_pkg)
    return n
