"""Browser session + pool Protocols.

The interpreter and capabilities both program against `BrowserSession` —
a narrow async interface that maps closely to the browser primitives in
the registry. Two implementations:

  - `PlaywrightBrowserSession` (this package) — production, Chromium.
  - `FakeBrowserSession` — in-memory, scriptable, used by tests.

`BrowserPool` is the source of fresh sessions. v1's pool implementations
are simple (no pre-warming); a warm-pool optimization can drop in later
without the caller knowing.

Per saved decisions: per-run isolation, no profile reuse across runs.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class DownloadedFile:
    """Bytes the session captured from a download trigger or URL."""

    filename: str
    content: bytes


@dataclass(frozen=True, slots=True)
class ExtractedValue:
    value: str


class BrowserSession(Protocol):
    """A live browser context for a single run.

    All methods are async to allow the underlying Playwright implementation
    to use its async API. `close()` releases the browser context; the pool
    is responsible for lifecycle (don't call close() from activity code —
    let the pool's checkout context manager handle it).
    """

    @property
    def id(self) -> str: ...

    async def navigate(self, url: str) -> None: ...

    async def wait_for(self, selector: str, timeout_ms: int = 30000) -> None: ...

    async def fill(self, selector: str, value: str) -> None: ...

    async def click(self, selector: str) -> None: ...

    async def select(self, selector: str, value: str) -> None: ...

    async def upload(self, selector: str, file_path: str) -> None: ...

    async def download(
        self, *, trigger_selector: str | None = None, url: str | None = None
    ) -> DownloadedFile: ...

    async def extract(self, selector: str, attribute: str = "text") -> ExtractedValue: ...

    async def screenshot(self) -> bytes: ...

    async def screenshot_element(self, selector: str) -> bytes: ...
    """Bytes of just the element matched by `selector`. Used by HITL flows
    (e.g. captchas) so the user only sees the relevant region."""

    async def close(self) -> None: ...


class BrowserPool(Protocol):
    """Source of fresh sessions, one per checkout."""

    @asynccontextmanager
    def checkout(self, *, profile: str | None = None) -> AsyncIterator[BrowserSession]:
        # Protocol classes can't actually define context managers, so this
        # is a marker. Implementations decorate with @asynccontextmanager.
        raise NotImplementedError  # pragma: no cover
        yield  # type: ignore[unreachable]
