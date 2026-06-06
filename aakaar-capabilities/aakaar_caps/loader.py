"""Discover shared capabilities: every non-underscore module under
``aakaar_caps.caps`` that exposes ``SPEC`` + ``async def run(ctx, inputs)``."""

from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Awaitable, Callable
from typing import Any

from aakaar_caps.context import CapabilityContext
from aakaar_caps.spec import CapabilitySpec

CapabilityRun = Callable[[CapabilityContext, dict[str, Any]], Awaitable[dict[str, Any]]]


def load_specs() -> list[tuple[CapabilitySpec, CapabilityRun]]:
    import aakaar_caps.caps as pkg

    out: list[tuple[CapabilitySpec, CapabilityRun]] = []
    for _finder, name, _is_pkg in pkgutil.iter_modules(pkg.__path__, prefix="aakaar_caps.caps."):
        short = name.rsplit(".", 1)[-1]
        if short.startswith("_"):
            continue
        module = importlib.import_module(name)
        spec = getattr(module, "SPEC", None)
        run = getattr(module, "run", None)
        if spec is not None and callable(run):
            out.append((spec, run))
    return out
