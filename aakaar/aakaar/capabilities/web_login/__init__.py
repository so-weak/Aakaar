"""cap.web_login moved to the shared capability library
(``aakaar_caps.caps.web_login``) so the SAME code runs on the server and a
remote agent (credentials via ctx.secrets, captcha via the proxied HITL channel,
selector disambiguation via the proxied planner).

This module re-exports ``CAP_REF`` for back-compat. It exposes no
``definition``/``handler`` — the loader skips it and ``register_shared``
registers the shared implementation (server activity handler + catalog entry).
"""

from aakaar_caps.caps.web_login import CAP_REF  # noqa: F401

__all__ = ["CAP_REF"]
