"""Staff-authored capabilities live here.

Each capability is a Python module that exposes a `definition` (the
`CapabilityDefinition` registered with the registry) and a `handler` (the
async function the interpreter invokes when the DAG references the
capability's ref).

Loading: `load_into(registry, activities)` walks the package and registers
every capability found. The intent is "drop a folder, run the loader, the
capability is live tenant-side once granted."
"""

from __future__ import annotations

from aakaar.capabilities._base import (
    CapabilityHandler,
    CapabilityModule,
    load_into,
)

__all__ = ["CapabilityHandler", "CapabilityModule", "load_into"]
