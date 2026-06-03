"""Render each source MD to HTML, load in a headless browser, and report
any mermaid blocks that failed to parse (mermaid emits an SVG containing
the literal text "Syntax error in text" on parse failure)."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from build_pdfs import _HTML_TEMPLATE, _render_md, DOCS, SRC


async def _check_one(name: str) -> int:
    from playwright.async_api import async_playwright

    md_text = (SRC / name).read_text(encoding="utf-8")
    body = _render_md(md_text)
    html = (
        _HTML_TEMPLATE
        .replace("__TITLE__", Path(name).stem)
        .replace("__BODY__", body)
    )

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            await page.set_content(html, wait_until="networkidle")
            try:
                await page.wait_for_function(
                    "window.__mermaidReady === true", timeout=30000
                )
            except Exception:
                pass
            errors = await page.evaluate(
                """() => {
                    const blocks = Array.from(document.querySelectorAll('.mermaid'));
                    const failed = [];
                    blocks.forEach((el, i) => {
                        const txt = el.textContent || '';
                        if (txt.includes('Syntax error') || el.querySelector('text[style*="color"][style*="red"]')) {
                            failed.push({ index: i, snippet: txt.slice(0, 200) });
                        }
                    });
                    return { total: blocks.length, failed };
                }"""
            )
            return errors
        finally:
            await browser.close()


async def main() -> int:
    bad = 0
    for name in DOCS:
        result = await _check_one(name)
        status = "ok" if not result["failed"] else "BAD"
        print(f"{name}: {status} ({result['total']} mermaid blocks)")
        for f in result["failed"]:
            bad += 1
            print(f"  block #{f['index']}: {f['snippet'][:160]}...")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
