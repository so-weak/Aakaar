"""Shared capability library.

A capability is declared once here (a ``SPEC`` + an ``async def run(ctx, inputs)``)
and can run in either host:
  - the SERVER wraps it as a normal capability (its ActivityContext supplies
    secrets from the vault, object storage, and the LLM), or
  - a remote AGENT runs the same code with a lightweight context (secrets come
    from the dispatch envelope; object/LLM services may be absent).

Capabilities depend only on the small ``CapabilityContext`` surface, so the same
code is portable across both hosts. Where it runs is a placement decision.
"""

from aakaar_caps.context import CapabilityContext, CapabilityError
from aakaar_caps.loader import load_specs
from aakaar_caps.spec import CapabilitySpec

__all__ = ["CapabilityContext", "CapabilityError", "CapabilitySpec", "load_specs"]
