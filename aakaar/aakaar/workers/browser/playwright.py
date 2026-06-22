"""Moved to ``aakaar_caps.browser.playwright`` (shared, agent-portable).

Re-exported here for back-compat. Playwright is still imported lazily inside
the pool, so importing this module never requires Playwright to be installed.
"""

from aakaar_caps.browser.playwright import (  # noqa: F401
    PlaywrightBrowserPool,
    PlaywrightBrowserSession,
)

__all__ = ["PlaywrightBrowserPool", "PlaywrightBrowserSession"]
