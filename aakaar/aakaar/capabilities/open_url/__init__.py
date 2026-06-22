"""cap.open_url moved to the shared capability library (``aakaar_caps.caps.open_url``)
so the SAME code runs on the server and a remote agent.

This module now only re-exports ``CAP_REF`` for back-compat with existing
imports. It deliberately exposes no ``definition``/``handler`` — the capability
loader skips it, and ``register_shared`` registers the shared implementation
(both as a server activity handler and into the catalog).
"""

from aakaar_caps.caps.open_url import CAP_REF  # noqa: F401

__all__ = ["CAP_REF"]
