"""Moved to ``aakaar_caps.browser.fake`` (shared). Re-exported for back-compat."""

from aakaar_caps.browser.fake import (  # noqa: F401
    FakeBrowserPool,
    FakeBrowserSession,
)

__all__ = ["FakeBrowserPool", "FakeBrowserSession"]
