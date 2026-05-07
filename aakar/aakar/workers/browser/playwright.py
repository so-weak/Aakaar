"""Playwright-backed BrowserSession + Pool.

Imported lazily — Playwright (and Chromium) is heavy. If the package is
not installed or browsers are not provisioned, importing this module
raises only when it's actually constructed, never at import time.

For v1: per-run isolation, no warm pool, headless by default. The warm
pool is a Phase 2 optimization once we have measurements.

This module is NOT exercised by the default test suite — those tests use
`FakeBrowserPool`. To run a smoke test against real Chromium:
  AAKAR_RUN_PLAYWRIGHT_TESTS=1 pytest -k playwright_smoke
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from aakar.workers.browser.session import (
    BrowserSession,
    DownloadedFile,
    ExtractedValue,
)


@dataclass
class PlaywrightBrowserSession(BrowserSession):
    """Wraps a Playwright BrowserContext + Page pair.

    Use `PlaywrightBrowserPool.checkout()` rather than constructing this
    directly so lifecycle is handled correctly.
    """

    _id: str
    page: Any  # playwright.async_api.Page (kept untyped to avoid hard import)
    context: Any  # playwright.async_api.BrowserContext

    @property
    def id(self) -> str:
        return self._id

    async def navigate(self, url: str) -> None:
        await self.page.goto(url)

    async def wait_for(self, selector: str, timeout_ms: int = 30000) -> None:
        await self.page.wait_for_selector(selector, timeout=timeout_ms)

    async def fill(self, selector: str, value: str) -> None:
        await self.page.fill(selector, value)

    async def click(self, selector: str) -> None:
        await self.page.click(selector)

    async def select(self, selector: str, value: str) -> None:
        await self.page.select_option(selector, value)

    async def upload(self, selector: str, file_path: str) -> None:
        await self.page.set_input_files(selector, file_path)

    async def download(
        self, *, trigger_selector: str | None = None, url: str | None = None
    ) -> DownloadedFile:
        if trigger_selector and url:
            raise ValueError("download accepts trigger_selector OR url, not both")
        if trigger_selector:
            async with self.page.expect_download() as info:
                await self.page.click(trigger_selector)
            download = await info.value
            path = await download.path()
            with open(path, "rb") as f:
                content = f.read()
            return DownloadedFile(filename=download.suggested_filename, content=content)
        if url:
            response = await self.page.context.request.get(url)
            return DownloadedFile(
                filename=url.rsplit("/", 1)[-1] or "download.bin",
                content=await response.body(),
            )
        raise ValueError("download requires trigger_selector or url")

    async def extract(self, selector: str, attribute: str = "text") -> ExtractedValue:
        if attribute == "text":
            value = await self.page.inner_text(selector)
        elif attribute == "html":
            value = await self.page.inner_html(selector)
        else:
            value = await self.page.get_attribute(selector, attribute) or ""
        return ExtractedValue(value=value)

    async def screenshot(self) -> bytes:
        return await self.page.screenshot(full_page=True)

    async def screenshot_element(self, selector: str) -> bytes:
        return await self.page.locator(selector).screenshot()

    async def close(self) -> None:
        try:
            await self.context.close()
        except Exception:
            # Closing twice or after the browser process died — ignore.
            pass


@dataclass
class PlaywrightBrowserPool:
    """Per-run, headless-by-default Chromium pool.

    Phase-2 work: warm pool, headed mode (Xvfb), per-tenant proxy.
    """

    headless: bool = True
    _playwright: Any = field(default=None, init=False)
    _browser: Any = field(default=None, init=False)

    async def _ensure_started(self) -> None:
        if self._browser is not None:
            return
        from playwright.async_api import async_playwright

        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(headless=self.headless)

    async def shutdown(self) -> None:
        if self._browser is not None:
            await self._browser.close()
            self._browser = None
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None

    @asynccontextmanager
    async def checkout(
        self, *, profile: str | None = None
    ) -> AsyncIterator[PlaywrightBrowserSession]:
        _ = profile  # v1: profiles ignored
        await self._ensure_started()
        context = await self._browser.new_context()
        page = await context.new_page()
        session = PlaywrightBrowserSession(
            _id=f"pw-{uuid.uuid4().hex[:8]}", page=page, context=context
        )
        try:
            yield session
        finally:
            await session.close()
