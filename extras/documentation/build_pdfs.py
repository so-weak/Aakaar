"""Build PDFs from documentation/src/*.md into documentation/descriptions/.

Pipeline: markdown-it-py renders Markdown to HTML, ```mermaid``` fences are
rewritten to <div class="mermaid"> blocks, then a headless Chromium loads the
page, waits for mermaid to finish, and prints to PDF.

Run from repo root:
    aakar/.venv/bin/python documentation/build_pdfs.py
"""

from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path

from markdown_it import MarkdownIt

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
OUT = ROOT / "descriptions"

DOCS = [
    "01-HLD.md",
    "02-LLD.md",
    "03-Backend-Architecture.md",
    "04-Frontend-Architecture.md",
    "05-Roadmap.md",
]

_HTML_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<title>__TITLE__</title>
<script src="https://cdn.jsdelivr.net/npm/mermaid@10/dist/mermaid.min.js"></script>
<style>
  @page { size: A4; margin: 18mm 16mm; }
  html, body { background: #fff; color: #111; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif; line-height: 1.5; font-size: 11pt; }
  body { max-width: 100%; padding: 0; margin: 0; }
  h1 { font-size: 22pt; margin-top: 0; border-bottom: 2px solid #0a4ea2; padding-bottom: 6px; }
  h2 { font-size: 16pt; margin-top: 24px; color: #0a4ea2; border-bottom: 1px solid #e5e7eb; padding-bottom: 3px; }
  h3 { font-size: 13pt; margin-top: 18px; color: #1f2937; }
  h4 { font-size: 11.5pt; margin-top: 14px; color: #374151; }
  p, li { font-size: 11pt; }
  code { background: #f4f4f5; padding: 1px 5px; border-radius: 3px; font-size: 10pt; font-family: "SF Mono", Menlo, Consolas, monospace; }
  pre { background: #0f172a; color: #e2e8f0; padding: 10px 12px; border-radius: 6px; overflow-x: auto; font-size: 9.5pt; line-height: 1.4; page-break-inside: avoid; }
  pre code { background: transparent; color: inherit; padding: 0; font-size: 9.5pt; }
  table { border-collapse: collapse; width: 100%; margin: 10px 0; font-size: 10pt; page-break-inside: avoid; }
  th, td { border: 1px solid #d4d4d8; padding: 6px 9px; text-align: left; vertical-align: top; }
  th { background: #f4f4f5; font-weight: 600; }
  blockquote { border-left: 3px solid #0a4ea2; margin: 10px 0; padding: 4px 12px; background: #f8fafc; color: #334155; }
  hr { border: 0; border-top: 1px solid #e5e7eb; margin: 20px 0; }
  .mermaid { text-align: center; margin: 14px 0; page-break-inside: avoid; }
  .mermaid svg { max-width: 100%; height: auto; }
  h1, h2, h3 { page-break-after: avoid; }
  ul, ol { padding-left: 22px; }
</style>
</head>
<body>
__BODY__
<script>
  mermaid.initialize({
    startOnLoad: false,
    theme: "neutral",
    flowchart: { htmlLabels: true, curve: "basis" },
    sequence: { mirrorActors: false, useMaxWidth: true },
    er: { useMaxWidth: true }
  });
  window.__mermaidReady = false;
  mermaid.run().then(function () { window.__mermaidReady = true; })
              .catch(function () { window.__mermaidReady = true; });
</script>
</body>
</html>
"""


def _render_md(md_text: str) -> str:
    """Render Markdown to HTML, converting ```mermaid``` fences to divs."""
    # Pull mermaid blocks out before markdown sees them so it doesn't escape
    # the contents.
    placeholders: list[str] = []

    def _stash(match: re.Match[str]) -> str:
        placeholders.append(match.group(1))
        return f"@@MERMAID_{len(placeholders) - 1}@@"

    stripped = re.sub(
        r"```mermaid\n(.*?)\n```",
        _stash,
        md_text,
        flags=re.DOTALL,
    )

    md = MarkdownIt("commonmark", {"html": False, "linkify": True, "typographer": True})
    md.enable("table")
    html = md.render(stripped)

    for i, body in enumerate(placeholders):
        # body must not be HTML-escaped; mermaid parses raw text.
        html = html.replace(
            f"@@MERMAID_{i}@@",
            f'<div class="mermaid">{body}</div>',
        )
    return html


async def _html_to_pdf(html: str, out_path: Path) -> None:
    from playwright.async_api import async_playwright

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
            await page.pdf(
                path=str(out_path),
                format="A4",
                print_background=True,
                margin={
                    "top": "18mm",
                    "right": "16mm",
                    "bottom": "18mm",
                    "left": "16mm",
                },
            )
        finally:
            await browser.close()


async def _build_one(src: Path, dst: Path) -> None:
    md_text = src.read_text(encoding="utf-8")
    body = _render_md(md_text)
    title = src.stem
    html = (
        _HTML_TEMPLATE
        .replace("__TITLE__", title)
        .replace("__BODY__", body)
    )
    await _html_to_pdf(html, dst)
    size_kb = dst.stat().st_size // 1024
    print(f"  wrote {dst.name} ({size_kb} KB)")


async def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    missing = [name for name in DOCS if not (SRC / name).exists()]
    if missing:
        print(f"missing source files: {missing}", file=sys.stderr)
        return 1
    print(f"building {len(DOCS)} PDFs into {OUT}")
    for name in DOCS:
        src = SRC / name
        dst = OUT / (Path(name).stem + ".pdf")
        await _build_one(src, dst)
    print("done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
