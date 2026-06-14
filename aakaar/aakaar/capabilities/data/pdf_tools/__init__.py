"""cap.pdf_tools — page-level PDF operations with pypdf.

Server-local, no-network, no-secrets capability. It reads one or more PDFs
the upstream graph already produced from object storage, performs a single
page-level operation, and writes any result PDFs back to object storage —
returning their `aakaar://` URIs (or, for `count_pages`, just the count).

`op` selects the operation:

  - count_pages:   read the (single) `sources` PDF and return its page count.
                   No PDF is written; `result_uris` is empty.
  - merge:         concatenate every PDF in `sources` (in the given order)
                   into one PDF. Returns a single result URI.
  - extract_pages: pull the pages named by `pages` out of the (single)
                   `sources` PDF, in the order given, into one new PDF.
                   Returns a single result URI.
  - split:         explode the (single) `sources` PDF into one PDF per page
                   (or, when `pages` is given, one PDF per named page).
                   Returns one result URI per page, in order.

`sources` is a list of `aakaar://` URIs. `merge` may take many; the other
ops take exactly one (the first is used and a clear error is raised if more
than one is supplied).

`pages` is a 1-based page selector — a list mixing single integers and
inclusive "start-end" range strings, e.g. [1, "3-5", 8]. Used by
`extract_pages` (required) and `split` (optional). Pages are validated
against the document and a clear RuntimeError is raised for out-of-range
or malformed entries.

`output_prefix` controls where results land under the run's object key
space; it defaults to a per-run pdf_tools folder. Output keys look like
`{prefix}/{uuid}.pdf` for single-file ops and `{prefix}/{uuid}-pNN.pdf`
for split parts.

pypdf is imported lazily inside the handler so module import never fails
when pypdf is absent; the handler raises a clear RuntimeError if it is
unavailable.
"""

from __future__ import annotations

import io
import logging
import uuid
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from aakaar.capabilities.data._pdf_pages import expand_entry, parse_page_selector
from aakaar.interpreter.activities.types import ActivityContext
from aakaar.shared.registry import CapabilityDefinition

logger = logging.getLogger(__name__)
CAP_REF = "cap.pdf_tools"

_OPS = ("merge", "split", "extract_pages", "count_pages")
_SINGLE_SOURCE_OPS = ("split", "extract_pages", "count_pages")


class _Inputs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    op: Literal["merge", "split", "extract_pages", "count_pages"] = Field(
        description=(
            "The page-level operation: 'merge' concatenates all sources into "
            "one PDF; 'split' explodes one PDF into one-PDF-per-page; "
            "'extract_pages' pulls the named pages into one PDF; "
            "'count_pages' returns the page count of one PDF."
        ),
    )
    sources: list[str] = Field(
        description=(
            "aakaar:// URIs of the input PDF(s). 'merge' accepts many (kept in "
            "order); the other ops require exactly one."
        ),
    )
    pages: list[int | str] | None = Field(
        default=None,
        description=(
            "1-based page selector mixing integers and inclusive 'start-end' "
            "range strings, e.g. [1, '3-5', 8]. Required for 'extract_pages'; "
            "optional for 'split' (limits which pages are emitted); ignored "
            "by 'merge' and 'count_pages'."
        ),
    )
    output_prefix: str | None = Field(
        default=None,
        description=(
            "Object-key prefix for result PDFs. Defaults to a per-run "
            "pdf_tools folder. Ignored by 'count_pages' (no output written)."
        ),
    )


class _Outputs(BaseModel):
    result_uris: list[str] = Field(
        description=(
            "aakaar:// URIs of the result PDF(s), in order. Empty for "
            "'count_pages'."
        ),
    )
    count: int = Field(
        description=(
            "For 'count_pages', the page count of the source PDF; otherwise "
            "the number of result PDFs written."
        ),
    )


definition = CapabilityDefinition(
    ref=CAP_REF,
    description=(
        "Page-level PDF operations with pypdf: merge several PDFs into one, "
        "split a PDF into one file per page, extract a selection of pages "
        "into a new PDF, or count the pages of a PDF. Reads inputs from and "
        "writes results to object storage. Server-local, no network, no "
        "credentials."
    ),
    input_schema=_Inputs,
    output_schema=_Outputs,
    secrets=(),
    tags=("data", "pdf", "document"),
)


# --------------------------------------------------------------------------
# Pure helpers (no ctx / no I/O) — unit-testable in isolation.
# --------------------------------------------------------------------------


def _parse_pages(pages: list[int | str] | None, page_count: int) -> list[int]:
    """Resolve a 1-based page selector into 0-based indices (shared helper)."""
    return parse_page_selector(pages, page_count, cap_ref=CAP_REF)


def _expand_entry(entry: int | str) -> list[int]:
    """Expand one page-selector entry into 1-based page numbers (shared helper)."""
    return expand_entry(entry, cap_ref=CAP_REF)


def _default_prefix(run_id: Any) -> str:
    return f"runs/{run_id}/pdf_tools"


def _output_key(prefix: str, suffix: str = "") -> str:
    base = prefix.strip("/")
    name = f"{uuid.uuid4().hex}{suffix}.pdf"
    return f"{base}/{name}" if base else name


def _require_single_source(op: str, sources: list[str]) -> str:
    if len(sources) != 1:
        raise RuntimeError(
            f"cap.pdf_tools: op {op!r} requires exactly one source, got "
            f"{len(sources)}"
        )
    return sources[0]


def _reader_from_bytes(raw: bytes) -> Any:
    import pypdf

    try:
        return pypdf.PdfReader(io.BytesIO(raw))
    except Exception as e:
        raise RuntimeError(f"cap.pdf_tools: could not read PDF: {e}") from e


def _writer_bytes(writer: Any) -> bytes:
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


# --------------------------------------------------------------------------
# Handler
# --------------------------------------------------------------------------


async def handler(ctx: ActivityContext, inputs: dict[str, Any]) -> dict[str, Any]:
    try:
        import pypdf  # noqa: F401
    except Exception as e:
        raise RuntimeError(
            "cap.pdf_tools requires the 'pypdf' package, which is not "
            f"available in this environment: {e}"
        ) from e

    op = inputs["op"]
    sources = list(inputs.get("sources") or [])
    if op not in _OPS:
        raise RuntimeError(f"cap.pdf_tools: unsupported op {op!r}")
    if not sources:
        raise RuntimeError("cap.pdf_tools: `sources` must not be empty")
    pages = inputs.get("pages")
    prefix = inputs.get("output_prefix") or _default_prefix(ctx.run_id)

    logger.info(
        "cap.pdf_tools start run_id=%s op=%s sources=%d",
        ctx.run_id,
        op,
        len(sources),
    )

    if op == "count_pages":
        uri = _require_single_source(op, sources)
        reader = _reader_from_bytes(ctx.object_store.get(uri))
        count = len(reader.pages)
        logger.info(
            "cap.pdf_tools ok run_id=%s op=count_pages count=%d", ctx.run_id, count
        )
        return {"result_uris": [], "count": count}

    if op == "merge":
        result_uris = [_do_merge(ctx, sources, prefix)]
    elif op == "extract_pages":
        result_uris = [_do_extract_pages(ctx, sources, pages, prefix)]
    elif op == "split":
        result_uris = _do_split(ctx, sources, pages, prefix)
    else:  # pragma: no cover - guarded above
        raise RuntimeError(f"cap.pdf_tools: unsupported op {op!r}")

    logger.info(
        "cap.pdf_tools ok run_id=%s op=%s outputs=%d",
        ctx.run_id,
        op,
        len(result_uris),
    )
    return {"result_uris": result_uris, "count": len(result_uris)}


def _do_merge(ctx: ActivityContext, sources: list[str], prefix: str) -> str:
    import pypdf

    writer = pypdf.PdfWriter()
    for uri in sources:
        reader = _reader_from_bytes(ctx.object_store.get(uri))
        for page in reader.pages:
            writer.add_page(page)
    out_bytes = _writer_bytes(writer)
    key = _output_key(prefix)
    obj = ctx.object_store.put(str(ctx.tenant_id), key, out_bytes)
    return obj.uri


def _do_extract_pages(
    ctx: ActivityContext,
    sources: list[str],
    pages: list[int | str] | None,
    prefix: str,
) -> str:
    import pypdf

    if not pages:
        raise RuntimeError("cap.pdf_tools: op 'extract_pages' requires `pages`")
    uri = _require_single_source("extract_pages", sources)
    reader = _reader_from_bytes(ctx.object_store.get(uri))
    indices = _parse_pages(pages, len(reader.pages))
    writer = pypdf.PdfWriter()
    for idx in indices:
        writer.add_page(reader.pages[idx])
    out_bytes = _writer_bytes(writer)
    key = _output_key(prefix)
    obj = ctx.object_store.put(str(ctx.tenant_id), key, out_bytes)
    return obj.uri


def _do_split(
    ctx: ActivityContext,
    sources: list[str],
    pages: list[int | str] | None,
    prefix: str,
) -> list[str]:
    import pypdf

    uri = _require_single_source("split", sources)
    reader = _reader_from_bytes(ctx.object_store.get(uri))
    page_count = len(reader.pages)
    indices = _parse_pages(pages, page_count) if pages else list(range(page_count))
    if not indices:
        raise RuntimeError("cap.pdf_tools: op 'split' produced no pages")
    out: list[str] = []
    for ordinal, idx in enumerate(indices, start=1):
        writer = pypdf.PdfWriter()
        writer.add_page(reader.pages[idx])
        out_bytes = _writer_bytes(writer)
        key = _output_key(prefix, suffix=f"-p{ordinal:02d}")
        obj = ctx.object_store.put(str(ctx.tenant_id), key, out_bytes)
        out.append(obj.uri)
    return out
