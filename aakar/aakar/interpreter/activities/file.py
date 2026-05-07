"""file.* — read/write structured files held in managed storage.

CSV reading goes through Python's stdlib `csv` module. Excel support is
deferred until a workflow actually needs it (avoids the openpyxl dep
weight).
"""

from __future__ import annotations

import csv
import io
from typing import Any

from aakar.interpreter.activities.registry import ActivityRegistry
from aakar.interpreter.activities.types import ActivityContext
from aakar.storage.object_store import parse_uri


async def parse_csv(ctx: ActivityContext, inputs: dict[str, Any]) -> dict[str, Any]:
    file_uri = inputs["file_uri"]
    delimiter = inputs.get("delimiter", ",")
    has_header = bool(inputs.get("has_header", True))

    data = ctx.object_store.get(file_uri)
    text = data.decode("utf-8")
    if has_header:
        reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
        rows = [dict(r) for r in reader]
    else:
        reader = csv.reader(io.StringIO(text), delimiter=delimiter)
        rows = [{f"c{i}": v for i, v in enumerate(row)} for row in reader]
    return {"rows": rows}


async def write_csv(ctx: ActivityContext, inputs: dict[str, Any]) -> dict[str, Any]:
    rows: list[dict[str, Any]] = inputs["rows"]
    file_uri: str = inputs["file_uri"]
    if not rows:
        # Empty CSV is allowed but produces an empty file — there are no headers
        # to infer. Be explicit so the next reader doesn't trip.
        ctx.object_store.put(*parse_uri(file_uri), b"")
        return {"file_uri": file_uri}

    fieldnames = list(rows[0].keys())
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fieldnames)
    writer.writeheader()
    for row in rows:
        writer.writerow({k: row.get(k, "") for k in fieldnames})
    tenant_id, key = parse_uri(file_uri)
    ctx.object_store.put(tenant_id, key, buf.getvalue().encode("utf-8"))
    return {"file_uri": file_uri}


def register_into(reg: ActivityRegistry) -> None:
    reg.register("file.parse_csv", parse_csv)
    reg.register("file.write_csv", write_csv)
