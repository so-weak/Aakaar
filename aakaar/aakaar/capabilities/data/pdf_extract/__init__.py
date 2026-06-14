"""cap.pdf_extract — pull text out of a stored PDF, whole or per-page.

Server-local, no-network, no-secrets capability. It reads one PDF the
upstream graph already produced from object storage (an ``aakaar://``
URI — typically from ``cap.file_download`` or an extracted email
attachment) and returns its text both joined (``text``) and page-by-page
(``pages``), so a downstream node can cite or re-chunk by page.

``pages`` selects which pages to extract — the same 1-based selector
shape as ``cap.pdf_tools``, a list mixing single integers and inclusive
"start-end" range strings, e.g. ``[1, "3-5", 8]``. Omitted/null means
every page, in order.

``max_pages`` is the resource guard: extraction stops after that many
pages and the output's ``truncated`` flag is set, so a thousand-page
scan can't stall the run or flood the timeline. Out-of-range or
malformed selector entries still raise a clear RuntimeError — only the
*volume* is clamped, never silently re-interpreted.

pypdf is imported lazily inside the handler so module import (and
therefore describe/registry listing) never fails when the library is
absent; the handler raises a clear RuntimeError pointing at the ``doc``
extra if pypdf is unavailable at run time. Encrypted PDFs are attempted
with the empty owner password and refused with a clear error otherwise.
"""

from __future__ import annotations

import io
import logging
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from aakaar.capabilities.data._pdf_pages import parse_page_selector
from aakaar.interpreter.activities.types import ActivityContext
from aakaar.shared.registry import CapabilityDefinition

logger = logging.getLogger(__name__)
CAP_REF = "cap.pdf_extract"

_DEFAULT_MAX_PAGES = 50
_MAX_PAGES_CEILING = 500


class _Inputs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: str = Field(
        description="aakaar:// URI of the PDF to extract (produced upstream).",
    )
    pages: list[int | str] | None = Field(
        default=None,
        description=(
            "1-based page selector mixing integers and inclusive 'start-end' "
            "range strings, e.g. [1, '3-5', 8]. Null extracts every page in "
            "order."
        ),
    )
    max_pages: int = Field(
        default=_DEFAULT_MAX_PAGES,
        ge=1,
        le=_MAX_PAGES_CEILING,
        description=(
            "Stop after extracting this many pages and set `truncated` "
            f"instead of failing. Default {_DEFAULT_MAX_PAGES}, "
            f"ceiling {_MAX_PAGES_CEILING}."
        ),
    )


class _Page(BaseModel):
    page: int = Field(description="1-based page number within the source PDF.")
    text: str = Field(description="Extracted text of that page (may be empty).")


class _Outputs(BaseModel):
    page_count: int = Field(description="Total pages in the source PDF.")
    pages: list[_Page] = Field(
        description="Extracted pages in selection order (at most max_pages)."
    )
    text: str = Field(
        description="The extracted page texts joined with blank lines."
    )
    truncated: bool = Field(
        description=(
            "True when the selection had more pages than max_pages and the "
            "tail was dropped."
        ),
    )


definition = CapabilityDefinition(
    ref=CAP_REF,
    description=(
        "Extract text from a stored PDF with pypdf, returning the joined "
        "text plus a per-page breakdown. `pages` selects 1-based pages/"
        "ranges (e.g. [1, '3-5']); `max_pages` caps the volume, setting "
        "`truncated` rather than failing on huge documents. Server-local, "
        "no network, no credentials; requires pypdf (extra: 'doc')."
    ),
    input_schema=_Inputs,
    output_schema=_Outputs,
    secrets=(),
    tags=("data", "pdf", "document", "extract", "text"),
)


def _reader_from_bytes(raw: bytes) -> Any:
    import pypdf

    try:
        reader = pypdf.PdfReader(io.BytesIO(raw))
    except Exception as e:
        raise RuntimeError(f"cap.pdf_extract: could not read PDF: {e}") from e
    if reader.is_encrypted:
        # Many "encrypted" PDFs only set an owner password; the empty user
        # password opens them. Anything stronger needs a password we don't
        # have a channel for, so refuse clearly.
        try:
            if not reader.decrypt(""):
                raise RuntimeError(
                    "cap.pdf_extract: PDF is password-protected"
                )
        except RuntimeError:
            raise
        except Exception as e:
            raise RuntimeError(
                f"cap.pdf_extract: PDF is encrypted and could not be opened: {e}"
            ) from e
    return reader


def _page_text(reader: Any, index: int) -> str:
    try:
        return reader.pages[index].extract_text() or ""
    except Exception:
        # A malformed page shouldn't sink the whole document.
        logger.warning(
            "cap.pdf_extract: failed to extract text from page %d", index + 1,
            exc_info=True,
        )
        return ""


async def handler(ctx: ActivityContext, inputs: dict[str, Any]) -> dict[str, Any]:
    try:
        import pypdf  # noqa: F401
    except Exception as e:
        raise RuntimeError(
            "cap.pdf_extract requires the 'pypdf' package (install the "
            f"'doc' extra), which is not available in this environment: {e}"
        ) from e

    source = inputs["source"]
    selector = inputs.get("pages")
    max_pages = int(inputs.get("max_pages") or _DEFAULT_MAX_PAGES)
    if max_pages < 1 or max_pages > _MAX_PAGES_CEILING:
        raise RuntimeError(
            f"cap.pdf_extract: max_pages must be between 1 and "
            f"{_MAX_PAGES_CEILING}, got {max_pages}"
        )

    logger.info(
        "cap.pdf_extract start run_id=%s source=%s max_pages=%d",
        ctx.run_id,
        source,
        max_pages,
    )

    reader = _reader_from_bytes(ctx.object_store.get(source))
    page_count = len(reader.pages)
    if selector:
        indices = parse_page_selector(selector, page_count, cap_ref=CAP_REF)
    else:
        if page_count <= 0:
            raise RuntimeError("cap.pdf_extract: source PDF has no pages")
        # Build at most max_pages indices: an encrypted PDF reports its page
        # count from the untrusted /Pages /Count (pypdf trusts the hint on the
        # encrypted path rather than walking the tree), so a forged billion-page
        # /Count would make list(range(page_count)) allocate billions of ints
        # before the max_pages slice below could clamp it. Bound the build.
        indices = list(range(min(page_count, max_pages)))

    truncated = len(indices) > max_pages or (not selector and page_count > max_pages)
    indices = indices[:max_pages]

    texts = [_page_text(reader, idx) for idx in indices]
    pages = [
        {"page": idx + 1, "text": page_text}
        for idx, page_text in zip(indices, texts, strict=True)
    ]
    text = "\n\n".join(t for t in texts if t)

    logger.info(
        "cap.pdf_extract ok run_id=%s source=%s pages=%d/%d truncated=%s chars=%d",
        ctx.run_id,
        source,
        len(pages),
        page_count,
        truncated,
        len(text),
    )
    return {
        "page_count": page_count,
        "pages": pages,
        "text": text,
        "truncated": truncated,
    }
