"""Login-form auto-discovery moved to ``aakaar_caps.caps.web_login.discovery``
(portable — it only drives a BrowserSession via JS). Re-exported here so
existing ``aakaar.capabilities.web_login.discovery`` imports (e.g. the agentic
planner's tool runner) keep working unchanged.
"""

from aakaar_caps.caps.web_login.discovery import (  # noqa: F401
    LoginFormDescriptor,
    discover_login_form,
)

__all__ = ["LoginFormDescriptor", "discover_login_form"]
