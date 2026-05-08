"""Fake browser session + pool for tests.

Records every method call so tests can assert on what a capability did
without running a real browser. Programmable behavior:

  - `script_extract({selector: value})` queues responses for `extract()`
  - `script_download({trigger_selector_or_url: (filename, bytes)})` queues
    responses for `download()`
  - `wait_failures: set[str]` causes `wait_for(selector)` to raise

Sessions are independent — one per `checkout()`.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from aakar.workers.browser.session import DownloadedFile, ExtractedValue


@dataclass
class FakeBrowserSession:
    """In-memory session. All methods record into `calls`."""

    id_: str = field(default_factory=lambda: f"fake-{uuid.uuid4().hex[:8]}")
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    # Programmable per-session behavior — set before handing the session out.
    extract_responses: dict[str, str] = field(default_factory=dict)
    download_responses: dict[str, tuple[str, bytes]] = field(default_factory=dict)
    element_screenshot_responses: dict[str, bytes] = field(default_factory=dict)
    evaluate_responses: dict[str, object] = field(default_factory=dict)
    wait_failures: set[str] = field(default_factory=set)
    closed: bool = False

    @property
    def id(self) -> str:
        return self.id_

    async def navigate(self, url: str) -> None:
        self.calls.append(("navigate", {"url": url}))

    async def wait_for(
        self,
        selector: str,
        timeout_ms: int = 30000,
        state: str = "attached",
    ) -> None:
        self.calls.append(
            ("wait_for", {"selector": selector, "timeout_ms": timeout_ms, "state": state})
        )
        # Tests can fail attached- and detached-waits independently by
        # adding `selector` (or `selector#detached`) to wait_failures.
        if selector in self.wait_failures:
            raise TimeoutError(f"wait_for({selector!r}) failed in fake")

    async def fill(self, selector: str, value: str) -> None:
        self.calls.append(("fill", {"selector": selector, "value": value}))

    async def click(self, selector: str) -> None:
        self.calls.append(("click", {"selector": selector}))

    async def select(self, selector: str, value: str) -> None:
        self.calls.append(("select", {"selector": selector, "value": value}))

    async def upload(self, selector: str, file_path: str) -> None:
        self.calls.append(("upload", {"selector": selector, "file_path": file_path}))

    async def download(
        self, *, trigger_selector: str | None = None, url: str | None = None
    ) -> DownloadedFile:
        key = trigger_selector or url or ""
        self.calls.append(("download", {"trigger_selector": trigger_selector, "url": url}))
        if key in self.download_responses:
            filename, content = self.download_responses[key]
            return DownloadedFile(filename=filename, content=content)
        return DownloadedFile(filename="fake.bin", content=b"fake-bytes")

    async def extract(self, selector: str, attribute: str = "text") -> ExtractedValue:
        self.calls.append(("extract", {"selector": selector, "attribute": attribute}))
        return ExtractedValue(value=self.extract_responses.get(selector, ""))

    async def screenshot(self) -> bytes:
        self.calls.append(("screenshot", {}))
        return b"\x89PNG\r\n"  # PNG header — enough to look real-ish

    async def screenshot_element(self, selector: str) -> bytes:
        self.calls.append(("screenshot_element", {"selector": selector}))
        return self.element_screenshot_responses.get(selector, b"\x89PNG\r\nfake-element")

    async def evaluate(self, js: str) -> object:
        self.calls.append(("evaluate", {"js": js}))
        # Tests pre-load `evaluate_responses` keyed by a marker substring of
        # the JS payload (so they don't have to match the whole script).
        for marker, value in self.evaluate_responses.items():
            if marker in js:
                return value
        return None

    async def close(self) -> None:
        self.calls.append(("close", {}))
        self.closed = True


@dataclass
class FakeBrowserPool:
    """Pool that hands out FakeBrowserSession per checkout.

    `next_sessions` is a queue: tests can push pre-configured sessions for
    a run that exercises specific behavior. If empty, a fresh default
    session is created.

    `handed_out` records every session ever returned; useful for asserting
    "exactly one session per run".
    """

    next_sessions: list[FakeBrowserSession] = field(default_factory=list)
    handed_out: list[FakeBrowserSession] = field(default_factory=list)

    @asynccontextmanager
    async def checkout(
        self, *, profile: str | None = None
    ) -> AsyncIterator[FakeBrowserSession]:
        _ = profile  # ignored by the fake
        sess = self.next_sessions.pop(0) if self.next_sessions else FakeBrowserSession()
        self.handed_out.append(sess)
        try:
            yield sess
        finally:
            await sess.close()
