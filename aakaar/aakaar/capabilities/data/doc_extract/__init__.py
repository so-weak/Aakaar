"""cap.doc_extract — read a stored document and return its structured content.

Server-local, read-only extraction over a single object in the tenant's
object store (an ``aakaar://`` URI the upstream graph already produced —
typically from ``cap.file_download`` or an email attachment). The handler
dispatches on the file's extension and returns a typed result:

  - csv  -> {"type": "csv",  "data": [<row dict>, ...]}
  - xlsx -> {"type": "xlsx", "data": [<row dict>, ...]}  (first / named sheet)
  - json -> {"type": "json", "data": <parsed JSON>}
  - pdf  -> {"type": "pdf",  "text": "<page text>", "tables": [[...rows...]]}
  - txt  -> {"type": "txt",  "text": "<decoded text>"}  (default for unknown)

Optional LLM pass: when ``inputs.extract`` is set *and* ``ctx.llm`` is
available, the handler hands the extracted text/rows to
``ctx.llm.complete_text`` with the caller's instruction and returns the
model's structured JSON under ``extracted`` (alongside the raw result).
The LLM is used only for narrow read-only extraction on already-fetched
content, never for action selection, so this stays on the right side of
the planner/executor spine. Empty / unparseable model output, or a missing
``ctx.llm``, degrades gracefully: ``extracted`` is simply omitted.

Heavy parsers (pandas, openpyxl, pypdf) are imported lazily inside the
handler so importing this module never fails when a library is absent; a
clear RuntimeError is raised if a parser required for the given extension
is unavailable. No secrets, no network.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from aakaar.interpreter.activities.types import ActivityContext
from aakaar.shared.registry import CapabilityDefinition

logger = logging.getLogger(__name__)
CAP_REF = "cap.doc_extract"

# How much extracted text/rows to feed the optional LLM pass. Bounded so a
# large document doesn't blow the model's context window.
_LLM_CONTENT_CHARS = 12000


class _Inputs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    uri: str = Field(
        description="aakaar:// URI of the document to extract (produced upstream).",
    )
    format: str | None = Field(
        default=None,
        description=(
            "Override the parser: one of 'csv', 'xlsx', 'json', 'pdf', 'txt'. "
            "When null the extension of the URI key decides; unknown "
            "extensions are read as plain text."
        ),
    )
    sheet: str | None = Field(
        default=None,
        description=(
            "For xlsx: the worksheet name to read. Null reads the first sheet."
        ),
    )
    encoding: str = Field(
        default="utf-8",
        description="Text decoding for csv/txt content. Decoding is error-tolerant.",
    )
    extract: str | None = Field(
        default=None,
        description=(
            "Optional plain-language instruction. When set and an LLM is "
            "configured, the extracted text/rows are passed to the model and "
            "its structured JSON is returned under 'extracted'. Ignored when "
            "no LLM is available."
        ),
    )


class _Outputs(BaseModel):
    type: str = Field(description="The parser used: csv | xlsx | json | pdf | txt.")
    data: Any = Field(
        default=None,
        description="Structured rows (csv/xlsx) or parsed value (json). Null for pdf/txt.",
    )
    text: str | None = Field(
        default=None,
        description="Extracted text for pdf/txt modes. Null for csv/xlsx/json.",
    )
    tables: list[Any] | None = Field(
        default=None,
        description="Best-effort tables (list of row-lists) extracted from a PDF.",
    )
    extracted: Any = Field(
        default=None,
        description=(
            "LLM-structured result when inputs.extract was set and a model was "
            "available; otherwise null."
        ),
    )


definition = CapabilityDefinition(
    ref=CAP_REF,
    description=(
        "Read a stored document from the object store and return its content "
        "structured by file type: csv/xlsx as row objects, json parsed, pdf as "
        "text plus best-effort tables, everything else as plain text. Optionally "
        "runs a read-only LLM extraction pass when an instruction and model are "
        "supplied. Lazy parser imports; no secrets, no network."
    ),
    input_schema=_Inputs,
    output_schema=_Outputs,
    secrets=(),
    tags=("data", "document", "extract", "csv", "xlsx", "pdf", "json"),
)


# ---------------------------------------------------------------------------
# Format detection
# ---------------------------------------------------------------------------

_KNOWN_FORMATS = {"csv", "xlsx", "json", "pdf", "txt"}
# Extensions that map onto a known parser; anything else falls back to txt.
_EXT_FORMAT = {
    "csv": "csv",
    "xlsx": "xlsx",
    "xlsm": "xlsx",
    "json": "json",
    "pdf": "pdf",
    "txt": "txt",
    "text": "txt",
    "log": "txt",
    "md": "txt",
}


def _key_from_uri(uri: str) -> str:
    """Return the object key (path within the tenant) for extension sniffing.

    Tolerant of plain keys/filenames too — anything after the last '/'.
    """
    return uri.rsplit("/", 1)[-1]


def _detect_format(uri: str, override: str | None) -> str:
    if override:
        fmt = override.strip().lower()
        if fmt not in _KNOWN_FORMATS:
            raise ValueError(
                f"cap.doc_extract: unsupported format override {override!r}; "
                f"expected one of {sorted(_KNOWN_FORMATS)}"
            )
        return fmt
    key = _key_from_uri(uri)
    ext = key.rsplit(".", 1)[-1].lower() if "." in key else ""
    return _EXT_FORMAT.get(ext, "txt")


# ---------------------------------------------------------------------------
# Per-format parsers (heavy imports are lazy, inside each parser)
# ---------------------------------------------------------------------------


def _decode(raw: bytes, encoding: str) -> str:
    return raw.decode(encoding or "utf-8", errors="replace")


def _parse_csv(raw: bytes, encoding: str) -> dict[str, Any]:
    """CSV -> list of row dicts. Uses pandas when present, else stdlib csv."""
    text = _decode(raw, encoding)
    if not text.strip():
        return {"type": "csv", "data": []}
    try:
        import pandas as pd
    except Exception:  # pragma: no cover - pandas is installed in v1
        pd = None
    if pd is not None:
        df = pd.read_csv(io.StringIO(text))
        # NaN -> None so the output is JSON-clean.
        rows = df.where(df.notna(), None).to_dict(orient="records")
        return {"type": "csv", "data": rows}
    reader = csv.DictReader(io.StringIO(text))
    return {"type": "csv", "data": [dict(r) for r in reader]}


def _parse_xlsx(raw: bytes, sheet: str | None) -> dict[str, Any]:
    try:
        import pandas as pd
    except Exception as e:  # pragma: no cover - pandas is installed in v1
        raise RuntimeError(
            "cap.doc_extract: pandas is required to read xlsx documents but "
            "is not installed"
        ) from e
    try:
        import openpyxl  # noqa: F401  (pandas needs it as the xlsx engine)
    except Exception as e:
        raise RuntimeError(
            "cap.doc_extract: openpyxl is required to read xlsx documents but "
            "is not installed"
        ) from e
    sheet_name: Any = sheet if sheet else 0
    df = pd.read_excel(io.BytesIO(raw), sheet_name=sheet_name)
    rows = df.where(df.notna(), None).to_dict(orient="records")
    return {"type": "xlsx", "data": rows}


def _parse_json(raw: bytes, encoding: str) -> dict[str, Any]:
    text = _decode(raw, encoding)
    try:
        value = json.loads(text)
    except (json.JSONDecodeError, ValueError) as e:
        raise RuntimeError(f"cap.doc_extract: document is not valid JSON: {e}") from e
    return {"type": "json", "data": value}


def _parse_pdf(raw: bytes) -> dict[str, Any]:
    try:
        import pypdf
    except Exception as e:
        raise RuntimeError(
            "cap.doc_extract: pypdf is required to read pdf documents but is "
            "not installed"
        ) from e
    reader = pypdf.PdfReader(io.BytesIO(raw))
    text_parts: list[str] = []
    tables: list[Any] = []
    for page in reader.pages:
        try:
            page_text = page.extract_text() or ""
        except Exception:
            # A malformed page shouldn't sink the whole document.
            logger.warning("cap.doc_extract: failed to extract text from a PDF page", exc_info=True)
            page_text = ""
        if page_text:
            text_parts.append(page_text)
            tables.extend(_pdf_tables_from_text(page_text))
    return {
        "type": "pdf",
        "text": "\n".join(text_parts),
        "tables": tables,
    }


# Whitespace runs of 2+ spaces (or a tab) are treated as column separators in
# the flat text pypdf produces. This is a best-effort heuristic, not a true
# table extractor; rows with a single column are not considered tabular.
_PDF_COL_SEP = re.compile(r"\s{2,}|\t")


def _pdf_tables_from_text(page_text: str) -> list[list[list[str]]]:
    """Group consecutive multi-column lines on a page into tables (row-lists)."""
    tables: list[list[list[str]]] = []
    current: list[list[str]] = []
    for line in page_text.splitlines():
        stripped = line.strip()
        cells = [c.strip() for c in _PDF_COL_SEP.split(stripped) if c.strip()]
        if len(cells) >= 2:
            current.append(cells)
        else:
            if len(current) >= 2:
                tables.append(current)
            current = []
    if len(current) >= 2:
        tables.append(current)
    return tables


def _parse_txt(raw: bytes, encoding: str) -> dict[str, Any]:
    return {"type": "txt", "text": _decode(raw, encoding)}


# ---------------------------------------------------------------------------
# Optional LLM extraction pass
# ---------------------------------------------------------------------------


def _content_for_llm(result: dict[str, Any]) -> str:
    """Render the parsed result into a bounded text blob for the model."""
    if result.get("text"):
        return str(result["text"])[:_LLM_CONTENT_CHARS]
    data = result.get("data")
    if data is not None:
        return json.dumps(data, default=str)[:_LLM_CONTENT_CHARS]
    return ""


def _strip_code_fence(raw: str) -> str:
    """Tolerate a model that wraps JSON in ```json ... ``` despite instructions."""
    s = raw.strip()
    if s.startswith("```"):
        s = s[3:]
        if "\n" in s:
            first, rest = s.split("\n", 1)
            if first.strip().lower() in {"", "json"}:
                s = rest
        if s.rstrip().endswith("```"):
            s = s.rstrip()[:-3]
    return s.strip()


def _loads_lenient(raw: str) -> Any:
    """Parse JSON, else the first balanced-looking {...} or [...] span."""
    s = _strip_code_fence(raw)
    try:
        return json.loads(s)
    except (json.JSONDecodeError, ValueError):
        pass
    for opener, closer in (("{", "}"), ("[", "]")):
        start = s.find(opener)
        end = s.rfind(closer)
        if 0 <= start < end:
            try:
                return json.loads(s[start : end + 1])
            except (json.JSONDecodeError, ValueError):
                continue
    raise ValueError("LLM output was not parseable JSON")


def _llm_extract(ctx: ActivityContext, instruction: str, content: str) -> Any:
    """Run the optional model pass; returns parsed JSON or None on any failure."""
    if not content.strip():
        return None
    system = (
        "You extract structured data from a document. Respond with ONLY valid "
        "JSON and nothing else (no prose, no code fences). Use the shape that "
        "best fits the instruction (an object of fields, or a list of rows). "
        "Do not invent values; omit fields that are absent."
    )
    user = (
        f"Instruction: {instruction}\n\n"
        f"--- DOCUMENT CONTENT START ---\n{content}\n--- DOCUMENT CONTENT END ---"
    )
    try:
        raw = ctx.llm.complete_text(system, user)
    except Exception:
        logger.warning(
            "cap.doc_extract run_id=%s: llm.complete_text raised; skipping LLM pass",
            ctx.run_id,
            exc_info=True,
        )
        return None
    if not raw or not raw.strip():
        return None
    try:
        return _loads_lenient(raw)
    except ValueError:
        logger.info(
            "cap.doc_extract run_id=%s: LLM output not parseable JSON; skipping",
            ctx.run_id,
        )
        return None


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


async def handler(ctx: ActivityContext, inputs: dict[str, Any]) -> dict[str, Any]:
    uri = inputs["uri"]
    fmt = _detect_format(uri, inputs.get("format"))
    encoding = inputs.get("encoding") or "utf-8"
    sheet = inputs.get("sheet")
    instruction = inputs.get("extract")

    logger.info(
        "cap.doc_extract start run_id=%s uri=%s format=%s",
        ctx.run_id,
        uri,
        fmt,
    )

    raw = ctx.object_store.get(uri)

    if fmt == "csv":
        result = _parse_csv(raw, encoding)
    elif fmt == "xlsx":
        result = _parse_xlsx(raw, sheet)
    elif fmt == "json":
        result = _parse_json(raw, encoding)
    elif fmt == "pdf":
        result = _parse_pdf(raw)
    else:  # txt + unknown
        result = _parse_txt(raw, encoding)

    # Normalize the output to the full _Outputs shape.
    out: dict[str, Any] = {
        "type": result["type"],
        "data": result.get("data"),
        "text": result.get("text"),
        "tables": result.get("tables"),
        "extracted": None,
    }

    if instruction:
        if ctx.llm is None:
            logger.info(
                "cap.doc_extract run_id=%s: extract requested but no llm configured; "
                "returning raw result only",
                ctx.run_id,
            )
        else:
            out["extracted"] = _llm_extract(ctx, instruction, _content_for_llm(result))

    logger.info(
        "cap.doc_extract ok run_id=%s uri=%s format=%s used_llm=%s",
        ctx.run_id,
        uri,
        fmt,
        out["extracted"] is not None,
    )
    return out
