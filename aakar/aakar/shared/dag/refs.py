"""Reference parsing for ${nodeId.field} expressions inside node inputs.

Refs let one node's output flow into a downstream node's input without the
LLM ever touching the underlying value. The format is intentionally narrow:

  ${alias}            — entire output of `alias`
  ${alias.field}      — the named field
  ${alias.field.sub}  — nested fields (resolver walks the path at runtime)

Only string values are scanned for refs. A ref must occupy the entire string;
embedding (`"hello ${a.b} world"`) is intentionally not supported in v1 — it
encourages the LLM to do string templating that's hard to validate. Use a
dedicated `string.format` action node when you need it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

REF_PATTERN = re.compile(r"^\$\{([a-zA-Z_][a-zA-Z0-9_]*(?:\.[a-zA-Z_][a-zA-Z0-9_]*)*)\}$")


class RefError(ValueError):
    """Raised when a ref string is malformed."""


@dataclass(frozen=True, slots=True)
class Ref:
    """A parsed reference. `alias` is either a node id or a node's outputs_as.

    `path` is the field path (possibly empty for ${alias}).
    """

    alias: str
    path: tuple[str, ...]

    @property
    def head(self) -> str:
        """First segment after the alias — the top-level output field, or '' if none."""
        return self.path[0] if self.path else ""


def is_ref(value: Any) -> bool:
    """True iff `value` is a string that looks like a complete ref expression."""
    return isinstance(value, str) and bool(REF_PATTERN.match(value))


def parse_ref(value: str) -> Ref:
    """Parse a single ref string. Raises RefError if malformed."""
    m = REF_PATTERN.match(value)
    if not m:
        raise RefError(f"not a valid ref: {value!r}")
    parts = m.group(1).split(".")
    return Ref(alias=parts[0], path=tuple(parts[1:]))


def parse_refs(inputs: Any) -> list[tuple[tuple[str | int, ...], Ref]]:
    """Walk an inputs structure (dict/list/scalar) and collect all refs.

    Returns a list of (json_path, Ref) tuples, where json_path is the location
    of the ref within the structure (mix of dict keys and list indices).
    Useful for both validation and runtime resolution.
    """
    found: list[tuple[tuple[str | int, ...], Ref]] = []
    _walk((), inputs, found)
    return found


def _walk(
    path: tuple[str | int, ...],
    value: Any,
    out: list[tuple[tuple[str | int, ...], Ref]],
) -> None:
    if isinstance(value, str):
        if is_ref(value):
            out.append((path, parse_ref(value)))
        return
    if isinstance(value, dict):
        for k, v in value.items():
            _walk((*path, k), v, out)
        return
    if isinstance(value, list):
        for i, v in enumerate(value):
            _walk((*path, i), v, out)
        return
