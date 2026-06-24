"""cap.csv_report — write report rows as a CSV into managed storage (the server).

Builds a CSV (header + rows) from a single ``row`` dict and/or a list of ``rows``
and stores it via the canonical object store, which lives on the server — so the
report is "sent to the server" and addressable by its returned ``aakaar://`` URI
(downloadable via the API). Side-effecting (it writes).
"""

from __future__ import annotations

import csv
import io
import logging
import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from aakaar_caps.context import CapabilityContext
from aakaar_caps.spec import CapabilitySpec

logger = logging.getLogger(__name__)
CAP_REF = "cap.csv_report"


class _Inputs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    filename: str = Field(default="report.csv", description="Logical filename for the stored CSV.")
    row: dict[str, Any] | None = Field(default=None, description="A single report row (column -> value).")
    rows: list[dict[str, Any]] | None = Field(default=None, description="Multiple report rows.")
    columns: list[str] | None = Field(default=None, description="Explicit column order (else inferred from keys).")


class _Outputs(BaseModel):
    uri: str = Field(description="Managed-storage URI of the stored CSV (sent to the server).")
    filename: str = Field(description="The stored filename.")
    rows_written: int = Field(description="Number of data rows written.")


SPEC = CapabilitySpec(
    ref=CAP_REF,
    description=(
        "Write report rows (a single `row` and/or a list of `rows`) as a CSV into managed storage "
        "on the server, returning its aakaar:// URI. Columns are inferred from the row keys unless "
        "`columns` is given. Use to persist a verification/reconciliation report to the server."
    ),
    input_schema=_Inputs,
    output_schema=_Outputs,
    secrets=(),
    tags=("report", "csv"),
    side_effecting=True,
)


async def run(ctx: CapabilityContext, inputs: dict[str, Any]) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    if inputs.get("row"):
        rows.append(dict(inputs["row"]))
    if inputs.get("rows"):
        rows.extend(dict(r) for r in inputs["rows"])
    if not rows:
        raise ValueError("cap.csv_report needs `row` or `rows`.")

    columns = inputs.get("columns")
    if not columns:
        seen: dict[str, None] = {}
        for r in rows:
            for k in r:
                seen.setdefault(str(k), None)
        columns = list(seen)

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    for r in rows:
        writer.writerow({c: r.get(c, "") for c in columns})
    data = buf.getvalue().encode("utf-8")

    filename = str(inputs.get("filename", "report.csv"))
    key = f"runs/{ctx.run_id}/reports/{uuid.uuid4().hex}_{filename}"
    uri = await ctx.write_object(key, data)
    logger.info("cap.csv_report wrote %d row(s) -> %s (%d bytes)", len(rows), uri, len(data))
    return {"uri": uri, "filename": filename, "rows_written": len(rows)}
