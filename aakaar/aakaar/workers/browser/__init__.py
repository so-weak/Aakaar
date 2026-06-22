"""Back-compat shim package. The browser runtime now lives in
``aakaar_caps.browser`` (shared between the server and a remote agent so the
exact same Playwright code runs on both). These names are re-exported so
existing ``aakaar.workers.browser`` imports keep working unchanged.
"""

from aakaar_caps.browser import (  # noqa: F401
    BrowserPool,
    BrowserSession,
    DownloadedFile,
    ExtractedValue,
    FakeBrowserPool,
    FakeBrowserSession,
)

__all__ = [
    "BrowserPool",
    "BrowserSession",
    "DownloadedFile",
    "ExtractedValue",
    "FakeBrowserPool",
    "FakeBrowserSession",
]
