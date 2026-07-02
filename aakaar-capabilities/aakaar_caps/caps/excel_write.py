"""cap.excel_write — build an .xlsx workbook from rows and store it.

Takes ``rows`` (a list of lists, or a list of dicts sharing keys), builds a
single-sheet workbook with openpyxl, writes it to managed storage via
``ctx.write_object`` and returns the resulting ``aakaar://`` URI. When rows are
dicts, the union of keys (first-seen order) becomes a header row. Produces an
artifact, so ``side_effecting=True``. openpyxl is imported lazily.
"""

from __future__ import annotations

import io
import logging
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from aakaar_caps.context import CapabilityContext
from aakaar_caps.spec import CapabilitySpec

logger = logging.getLogger(__name__)
CAP_REF = "cap.excel_write"


class _Inputs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rows: list[Any] = Field(
        description=(
            "Rows to write: either a list of lists (each inner list is a row of cell "
            "values) or a list of dicts (keys become a header row, one row per dict)."
        ),
    )
    sheet_name: str = Field(default="Sheet1", description="Name for the single worksheet.")
    header: list[str] | None = Field(
        default=None,
        description="Optional explicit header row prepended above list-of-list rows.",
    )
    filename: str = Field(default="report.xlsx", description="Base filename for the stored artifact.")


class _Outputs(BaseModel):
    report_uri: str = Field(description="Managed-storage URI (aakaar://...) of the produced .xlsx workbook.")
    row_count: int = Field(description="Number of data rows written (excluding any header).")


SPEC = CapabilitySpec(
    ref=CAP_REF,
    description=(
        "Build a single-sheet .xlsx workbook from rows (list-of-lists, or list-of-dicts whose "
        "keys become a header row) with openpyxl, store it in managed storage, and return the "
        "artifact URI. Optionally supply an explicit header and sheet name. Offline."
    ),
    input_schema=_Inputs,
    output_schema=_Outputs,
    secrets=(),
    tags=("office", "excel"),
    side_effecting=True,
)


def _require_openpyxl() -> Any:
    try:
        import openpyxl  # type: ignore[import-untyped]
    except ImportError as exc:  # pragma: no cover - exercised only when dep absent
        raise RuntimeError(
            "cap.excel_write needs openpyxl — install aakaar-capabilities[automation]"
        ) from exc
    return openpyxl


def _rows_to_grid(rows: list[Any], header: list[str] | None) -> tuple[list[list[Any]], int]:
    """Normalize the input into a 2-D grid; return (grid, data_row_count)."""
    if rows and all(isinstance(r, dict) for r in rows):
        cols: list[str] = []
        for r in rows:
            for k in r:
                if k not in cols:
                    cols.append(str(k))
        grid: list[list[Any]] = [cols]
        for r in rows:
            grid.append([r.get(c) for c in cols])
        return grid, len(rows)

    grid = [list(r) if isinstance(r, (list, tuple)) else [r] for r in rows]
    if header is not None:
        return [list(header), *grid], len(grid)
    return grid, len(grid)


async def run(ctx: CapabilityContext, inputs: dict[str, Any]) -> dict[str, Any]:
    openpyxl = _require_openpyxl()
    rows = inputs["rows"]
    sheet_name = str(inputs.get("sheet_name", "Sheet1"))
    header = inputs.get("header")
    filename = str(inputs.get("filename", "report.xlsx"))

    grid, data_rows = _rows_to_grid(rows, header)
    logger.info("cap.excel_write start run_id=%s rows=%d", ctx.run_id, data_rows)

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = sheet_name[:31] or "Sheet1"  # Excel caps sheet names at 31 chars
    for row in grid:
        ws.append(row)

    buf = io.BytesIO()
    wb.save(buf)
    uri = await ctx.write_object(filename, buf.getvalue())
    logger.info("cap.excel_write ok run_id=%s uri=%s rows=%d", ctx.run_id, uri, data_rows)
    return {"report_uri": uri, "row_count": data_rows}
