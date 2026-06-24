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

from contextlib import AbstractAsyncContextManager
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

    async def wait_for(
        self,
        selector: str,
        timeout_ms: int = 30000,
        state: str = "attached",
    ) -> None: ...
    """Wait for `selector` to reach `state` ('attached'/'visible'/'detached'/
    'hidden'). Default 'attached' matches the historical behavior. Login
    success-criteria use 'detached' — after a successful submit, the
    username field should disappear from the DOM."""

    async def fill(self, selector: str, value: str) -> None: ...

    async def click(self, selector: str) -> None: ...

    async def click_by_text(self, text: str) -> None: ...
    """Click any element whose visible text matches `text`. Use when
    you want to click a navigation link or button by its label
    (e.g. 'Recon Upload', 'Logout') without guessing CSS selectors."""

    async def press(self, selector: str, key: str) -> None: ...
    """Focus the element matched by `selector` and press a single key
    (e.g. 'Enter'). Used to submit a field via the keyboard — e.g.
    confirming a reject remark with Enter to advance to the next record."""

    async def select(self, selector: str, value: str) -> None: ...

    async def set_field(self, label: str, value: str) -> None: ...
    """Set a form control identified by its visible label text. The
    implementation dispatches by control type (select / input / radio /
    checkbox), so the planner can write `set_field("Switch Type",
    "Issuer")` without emitting a guessed CSS selector. Removes a whole
    category of LLM-hallucinated selector failures from agentic DAGs."""

    async def upload(self, selector: str, file_path: str) -> None: ...

    async def download(
        self, *, trigger_selector: str | None = None, url: str | None = None
    ) -> DownloadedFile: ...

    async def extract(self, selector: str, attribute: str = "text") -> ExtractedValue: ...

    async def screenshot(self) -> bytes: ...

    async def screenshot_element(self, selector: str) -> bytes: ...
    """Bytes of just the element matched by `selector`. Used by HITL flows
    (e.g. captchas) so the user only sees the relevant region."""

    async def evaluate(self, js: str) -> object: ...
    """Run a JS expression in the page context and return its (JSON-
    serializable) result. Used by capability self-discovery — e.g. finding
    the login form structure without forcing the user to provide selectors."""

    async def close(self) -> None: ...


class BrowserPool(Protocol):
    """Source of fresh sessions, one per checkout."""

    def checkout(
        self, *, profile: str | None = None
    ) -> AbstractAsyncContextManager[BrowserSession]:
        # Implementations decorate a coroutine with @asynccontextmanager, whose
        # result satisfies AbstractAsyncContextManager. Declared as a plain stub
        # here so the Protocol isn't itself a generator.
        ...
