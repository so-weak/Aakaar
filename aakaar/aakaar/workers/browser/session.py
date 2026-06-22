"""Moved to ``aakaar_caps.browser.session`` (shared, agent-portable).

Re-exported here so existing ``aakaar.workers.browser.session`` imports keep
working. The Protocols are host-agnostic; the server and a remote agent both
program against them.
"""

from aakaar_caps.browser.session import (  # noqa: F401
    BrowserPool,
    BrowserSession,
    DownloadedFile,
    ExtractedValue,
)

__all__ = ["BrowserPool", "BrowserSession", "DownloadedFile", "ExtractedValue"]
