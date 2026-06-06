"""document.* — parse and emit structured documents held in managed storage.

These are interpreter primitives (NOT capabilities): they take object-store
URIs in, produce plain JSON-serializable dicts out, and stay deterministic.
Heavy parsing deps (pandas, pypdf, openpyxl) are imported lazily inside each
handler so a deployment that never touches Excel/PDF doesn't pay the import
cost — mirroring how `file.parse_csv` keeps to the stdlib.

Activities:
  - document.parse_excel  : .xlsx -> rows (with sheet selection)
  - document.parse_json   : bytes -> object, optional dotted-path filter
  - document.parse_pdf    : .pdf -> text + tables (optional page range)
  - document.write_excel  : rows -> .xlsx written into managed storage
  - document.merge_files  : concat several CSV/Excel files into one

All reads/writes go through `ctx.object_store`; URIs are the canonical
handles passed around the DAG.
"""

from __future__ import annotations

import io
import logging
from typing import Any

from aakaar.interpreter.activities.registry import ActivityRegistry
from aakaar.interpreter.activities.types import ActivityContext
from aakaar.storage.object_store import parse_uri

logger = logging.getLogger(__name__)


def _jsonable(value: Any) -> Any:
    """Coerce pandas/NumPy scalars into JSON-serializable Python values.

    pandas hands back NaN for empty cells and numpy int/float scalars for
    numeric columns; neither round-trips cleanly through the DAG's JSON
    refs, so normalize here.
    """
    import math

    if value is None:
        return None
    # numpy scalars expose .item(); use it before the NaN check so we
    # compare against a plain float.
    item = getattr(value, "item", None)
    if callable(item):
        try:
            value = item()
        except (ValueError, TypeError):  # pragma: no cover - defensive
            return value
    if isinstance(value, float) and math.isnan(value):
        return None
    return value


def _rows_from_dataframe(df: Any) -> list[dict[str, Any]]:
    records = df.to_dict(orient="records")
    return [{str(k): _jsonable(v) for k, v in row.items()} for row in records]


# ---------- parse_excel ----------------------------------------------------


async def parse_excel(ctx: ActivityContext, inputs: dict[str, Any]) -> dict[str, Any]:
    """Read an .xlsx workbook into rows.

    inputs:
      file_uri (required) : managed-storage URI of the workbook
      sheet (optional)    : sheet name (str) or zero-based index (int);
                            defaults to the first sheet
      has_header (optional, default True): treat row 0 as column names
    """
    import pandas as pd

    file_uri = inputs["file_uri"]
    sheet = inputs.get("sheet", 0)
    has_header = bool(inputs.get("has_header", True))

    data = ctx.object_store.get(file_uri)
    header = 0 if has_header else None
    df = pd.read_excel(io.BytesIO(data), sheet_name=sheet, header=header, engine="openpyxl")
    if has_header:
        rows = _rows_from_dataframe(df)
    else:
        # No header: stable c0/c1/... column names like file.parse_csv.
        df = df.rename(columns={c: f"c{i}" for i, c in enumerate(df.columns)})
        rows = _rows_from_dataframe(df)

    logger.debug(
        "document.parse_excel uri=%s sheet=%r rows=%d has_header=%s",
        file_uri,
        sheet,
        len(rows),
        has_header,
    )
    return {"rows": rows, "row_count": len(rows)}


# ---------- parse_json -----------------------------------------------------


def _apply_dotted_path(obj: Any, path: str) -> Any:
    """Walk a dotted path through dicts/lists.

    Supports list indices as numeric segments (e.g. ``items.0.name``).
    Raises KeyError/IndexError when a segment is missing so the workflow
    author sees a clear failure rather than a silent None.
    """
    cur = obj
    for seg in path.split("."):
        if seg == "":
            continue
        if isinstance(cur, list):
            cur = cur[int(seg)]
        elif isinstance(cur, dict):
            cur = cur[seg]
        else:
            raise KeyError(f"cannot descend into {type(cur).__name__} at segment {seg!r}")
    return cur


async def parse_json(ctx: ActivityContext, inputs: dict[str, Any]) -> dict[str, Any]:
    """Parse a JSON document, optionally extracting a dotted sub-path.

    inputs:
      file_uri (optional) : managed-storage URI of the JSON document
      text (optional)     : raw JSON string (alternative to file_uri)
      path (optional)     : dotted path to extract, e.g. ``data.items.0.id``
    """
    import json

    if "file_uri" in inputs and inputs["file_uri"]:
        raw = ctx.object_store.get(inputs["file_uri"]).decode("utf-8")
    elif inputs.get("text") is not None:
        raw = inputs["text"]
    else:
        raise ValueError("parse_json requires either 'file_uri' or 'text'")

    obj = json.loads(raw)
    path = inputs.get("path")
    if path:
        obj = _apply_dotted_path(obj, str(path))
    logger.debug("document.parse_json path=%r", path)
    return {"value": obj}


# ---------- parse_pdf ------------------------------------------------------


async def parse_pdf(ctx: ActivityContext, inputs: dict[str, Any]) -> dict[str, Any]:
    """Extract text (and crude tables) from a PDF.

    inputs:
      file_uri (required)  : managed-storage URI of the PDF
      page_start (optional): 1-based first page (inclusive), default 1
      page_end (optional)  : 1-based last page (inclusive), default last
      extract_tables (optional, default False): split rows on whitespace
                            runs into a list-of-lists per page (best effort)
    """
    from pypdf import PdfReader

    file_uri = inputs["file_uri"]
    extract_tables = bool(inputs.get("extract_tables", False))

    data = ctx.object_store.get(file_uri)
    reader = PdfReader(io.BytesIO(data))
    n_pages = len(reader.pages)

    start = int(inputs.get("page_start", 1))
    end = int(inputs.get("page_end", n_pages))
    if start < 1:
        raise ValueError(f"page_start must be >= 1, got {start}")
    if end > n_pages:
        end = n_pages
    if start > end:
        raise ValueError(f"page_start {start} after page_end {end}")

    pages: list[dict[str, Any]] = []
    full_text_parts: list[str] = []
    for idx in range(start - 1, end):
        page = reader.pages[idx]
        text = page.extract_text() or ""
        full_text_parts.append(text)
        entry: dict[str, Any] = {"page": idx + 1, "text": text}
        if extract_tables:
            # Best-effort: each non-empty line becomes a row, columns split
            # on runs of 2+ spaces. PDFs have no real table structure in the
            # text layer, so this is intentionally simple.
            rows: list[list[str]] = []
            for line in text.splitlines():
                line = line.rstrip()
                if not line.strip():
                    continue
                import re

                cells = re.split(r"\s{2,}", line.strip())
                rows.append(cells)
            entry["tables"] = rows
        pages.append(entry)

    logger.debug(
        "document.parse_pdf uri=%s pages=%d range=%d-%d tables=%s",
        file_uri,
        len(pages),
        start,
        end,
        extract_tables,
    )
    return {
        "text": "\n".join(full_text_parts),
        "pages": pages,
        "page_count": n_pages,
    }


# ---------- write_excel ----------------------------------------------------


async def write_excel(ctx: ActivityContext, inputs: dict[str, Any]) -> dict[str, Any]:
    """Write rows to an .xlsx workbook in managed storage.

    inputs:
      file_uri (required) : managed-storage URI to write
      rows (required)     : list of dicts (column union preserves first-seen order)
      sheet_name (optional, default 'Sheet1')
    """
    import pandas as pd

    file_uri: str = inputs["file_uri"]
    rows: list[dict[str, Any]] = inputs["rows"]
    sheet_name: str = inputs.get("sheet_name", "Sheet1")

    # Preserve first-seen column order across all rows (dicts can vary).
    columns: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for key in row:
            if key not in seen:
                seen.add(key)
                columns.append(str(key))

    df = pd.DataFrame(rows, columns=columns or None)
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df.to_excel(writer, sheet_name=sheet_name, index=False)

    tenant_id, key = parse_uri(file_uri)
    obj = ctx.object_store.put(tenant_id, key, buf.getvalue())
    logger.info(
        "document.write_excel uri=%s rows=%d cols=%d sheet=%s",
        file_uri,
        len(rows),
        len(columns),
        sheet_name,
    )
    return {"file_uri": obj.uri, "row_count": len(rows)}


# ---------- merge_files ----------------------------------------------------


def _read_tabular(data: bytes, fmt: str, *, sheet: Any) -> Any:
    import pandas as pd

    if fmt == "csv":
        return pd.read_csv(io.BytesIO(data))
    if fmt in ("excel", "xlsx"):
        return pd.read_excel(io.BytesIO(data), sheet_name=sheet, engine="openpyxl")
    raise ValueError(f"unsupported merge format {fmt!r} (expected 'csv' or 'excel')")


def _infer_format(uri: str) -> str:
    lower = uri.lower()
    if lower.endswith((".xlsx", ".xls")):
        return "excel"
    return "csv"


async def merge_files(ctx: ActivityContext, inputs: dict[str, Any]) -> dict[str, Any]:
    """Concatenate several CSV/Excel files into a single output file.

    inputs:
      file_uris (required) : list of managed-storage URIs to concatenate
      output_uri (required): managed-storage URI for the merged result
      format (optional)    : 'csv' or 'excel'; inferred per-source and from
                             output extension when omitted
      sheet (optional)     : sheet selector applied to every Excel source
    """
    import pandas as pd

    file_uris: list[str] = inputs["file_uris"]
    output_uri: str = inputs["output_uri"]
    explicit_fmt = inputs.get("format")
    sheet = inputs.get("sheet", 0)

    if not file_uris:
        raise ValueError("merge_files requires at least one entry in 'file_uris'")

    frames = []
    for uri in file_uris:
        fmt = explicit_fmt or _infer_format(uri)
        data = ctx.object_store.get(uri)
        frames.append(_read_tabular(data, fmt, sheet=sheet))

    merged = pd.concat(frames, ignore_index=True)

    out_fmt = explicit_fmt or _infer_format(output_uri)
    tenant_id, key = parse_uri(output_uri)
    if out_fmt in ("excel", "xlsx"):
        buf = io.BytesIO()
        with pd.ExcelWriter(buf, engine="openpyxl") as writer:
            merged.to_excel(writer, index=False)
        payload = buf.getvalue()
    else:
        payload = merged.to_csv(index=False).encode("utf-8")

    obj = ctx.object_store.put(tenant_id, key, payload)
    logger.info(
        "document.merge_files sources=%d output=%s rows=%d fmt=%s",
        len(file_uris),
        output_uri,
        len(merged),
        out_fmt,
    )
    return {"file_uri": obj.uri, "row_count": int(len(merged))}


def register_into(reg: ActivityRegistry) -> None:
    reg.register("document.parse_excel", parse_excel)
    reg.register("document.parse_json", parse_json)
    reg.register("document.parse_pdf", parse_pdf)
    reg.register("document.write_excel", write_excel)
    reg.register("document.merge_files", merge_files)
