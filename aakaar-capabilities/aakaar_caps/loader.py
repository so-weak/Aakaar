"""Discover shared capabilities: every non-underscore module (or package) under
``aakaar_caps.caps`` that exposes ``SPEC`` + ``async def run(ctx, inputs)``.

``walk_packages`` (not ``iter_modules``) so a capability can be a *package*
(e.g. ``caps/web_login/`` with a ``discovery`` helper alongside its
``__init__``). Any path segment starting with ``_`` is skipped — those are
infrastructure helpers (``_shared``, ``_base``, a package's private submodules),
not capabilities. Helper submodules that simply lack ``SPEC`` are skipped too.
"""

from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Awaitable, Callable
from typing import Any

from aakaar_caps.context import CapabilityContext
from aakaar_caps.spec import CapabilitySpec

CapabilityRun = Callable[[CapabilityContext, dict[str, Any]], Awaitable[dict[str, Any]]]

_PREFIX = "aakaar_caps.caps."


def load_specs() -> list[tuple[CapabilitySpec, CapabilityRun]]:
    import aakaar_caps.caps as pkg

    out: list[tuple[CapabilitySpec, CapabilityRun]] = []
    for _finder, name, _is_pkg in pkgutil.walk_packages(pkg.__path__, prefix=_PREFIX):
        rel = name[len(_PREFIX):]
        # Skip private/infrastructure modules at any nesting level.
        if any(seg.startswith("_") for seg in rel.split(".")):
            continue
        module = importlib.import_module(name)
        spec = getattr(module, "SPEC", None)
        run = getattr(module, "run", None)
        if spec is not None and callable(run):
            out.append((spec, run))
        # A module may expose several capabilities via a `SPECS` list of
        # (CapabilitySpec, run) tuples — used where a family of small primitives
        # (e.g. the browser.* set) naturally lives in one module.
        specs = getattr(module, "SPECS", None)
        if specs:
            for s, r in specs:
                if s is not None and callable(r):
                    out.append((s, r))
    return out
