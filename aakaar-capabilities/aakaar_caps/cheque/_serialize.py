"""JSON-safe serialization for the cheque pipeline's dataclasses.

The pipeline returns rich dataclasses (``ChequeFields``, ``ChequeValidationReport``,
``ChequeDecision``, ``MicrResult``, ``SignatureResult``) that can nest tuples,
frozen dataclasses, and — critically — raw ``bytes`` (e.g. ``region_png`` on a
``SignatureResult``). Cap ``run`` functions must return a plain JSON-safe dict, so
this helper recursively:

  - prefers each object's own ``to_dict()`` when present (the pipeline's canonical
    projection — already bytes-free),
  - falls back to ``dataclasses.asdict`` for other dataclasses,
  - turns tuples/sets into lists and recurses into dict values / list items,
  - DROPS any ``bytes`` / ``bytearray`` value entirely (never return raw bytes to
    a caller — e.g. the cropped signature PNG), and
  - leaves JSON primitives (str/int/float/bool/None) untouched.

Starts with an underscore so ``aakaar_caps.loader`` skips it (not a capability).
"""

from __future__ import annotations

import dataclasses
from typing import Any

# Sentinel returned for values that must be dropped from the output (bytes).
_DROP = object()


def to_jsonsafe(obj: Any) -> Any:
    """Recursively convert ``obj`` into a JSON-serializable structure.

    Bytes-valued fields are dropped (returns the ``_DROP`` sentinel internally,
    which the dict/list walkers filter out). Never raises on the pipeline's own
    types; unknown objects fall back to ``str(obj)``.
    """
    result = _convert(obj)
    return None if result is _DROP else result


def _convert(obj: Any) -> Any:
    # Drop raw bytes outright — never surface them to a caller.
    if isinstance(obj, (bytes, bytearray)):
        return _DROP

    # JSON primitives pass straight through.
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj

    # Prefer the object's own canonical projection when it has one.
    to_dict = getattr(obj, "to_dict", None)
    if callable(to_dict) and not isinstance(obj, type):
        try:
            return _convert(to_dict())
        except Exception:  # noqa: BLE001 — fall through to generic handling
            pass

    # Dataclass instances (not classes) -> asdict, then recurse.
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return _convert(dataclasses.asdict(obj))

    # Mappings: recurse into values, dropping bytes-valued keys.
    if isinstance(obj, dict):
        out: dict[Any, Any] = {}
        for k, v in obj.items():
            cv = _convert(v)
            if cv is _DROP:
                continue
            out[k if isinstance(k, str) else str(k)] = cv
        return out

    # Sequences/sets -> list, dropping any bytes elements.
    if isinstance(obj, (list, tuple, set, frozenset)):
        return [cv for cv in (_convert(v) for v in obj) if cv is not _DROP]

    # Anything else (dates, Decimals, etc.) -> string for JSON safety.
    return str(obj)
