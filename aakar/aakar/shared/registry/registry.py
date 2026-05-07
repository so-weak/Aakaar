"""The Registry — an in-memory map of ref -> Definition.

The validator and planner both read from this. Entries are added at process
startup (capabilities loaded from the capabilities/ package; primitives from
builtins). The registry is immutable after the application has started; any
mutation in a request handler is a bug.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator

from aakar.shared.dag.types import NodeKind
from aakar.shared.registry.types import (
    ActionDefinition,
    CapabilityDefinition,
    ControlDefinition,
    Definition,
)


class RegistryConflict(ValueError):
    """Two definitions share the same ref."""


class Registry:
    """A flat ref -> Definition mapping with kind-aware accessors.

    Use `add()` during startup to register definitions, and `get()` /
    `capabilities()` / etc. at runtime to look them up. The validator only
    needs `get()`.
    """

    def __init__(self) -> None:
        self._defs: dict[str, Definition] = {}

    def add(self, defn: Definition) -> None:
        existing = self._defs.get(defn.ref)
        if existing is not None and existing is not defn:
            raise RegistryConflict(
                f"ref {defn.ref!r} is already registered ({type(existing).__name__})"
            )
        self._defs[defn.ref] = defn

    def add_many(self, defs: Iterable[Definition]) -> None:
        for d in defs:
            self.add(d)

    def get(self, ref: str) -> Definition | None:
        return self._defs.get(ref)

    def __contains__(self, ref: object) -> bool:
        return isinstance(ref, str) and ref in self._defs

    def __iter__(self) -> Iterator[Definition]:
        return iter(self._defs.values())

    def __len__(self) -> int:
        return len(self._defs)

    def by_kind(self, kind: NodeKind) -> list[Definition]:
        return [d for d in self._defs.values() if d.kind is kind]

    def capabilities(self) -> list[CapabilityDefinition]:
        return [d for d in self._defs.values() if isinstance(d, CapabilityDefinition)]

    def actions(self) -> list[ActionDefinition]:
        return [d for d in self._defs.values() if isinstance(d, ActionDefinition)]

    def controls(self) -> list[ControlDefinition]:
        return [d for d in self._defs.values() if isinstance(d, ControlDefinition)]
