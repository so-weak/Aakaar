"""file.* — read/write structured files held in managed storage.

CSV reading goes through Python's stdlib `csv` module. Excel support is
deferred until a workflow actually needs it (avoids the openpyxl dep
weight).

`file.read_local` is the bridge for "upload a file from my Downloads
folder" workflows: it ingests a local path into managed storage and
returns the URI. Gated behind `AAKAAR_ALLOW_LOCAL_PATHS=true` because a
DAG-emitted path is LLM-output and should not have unrestricted disk
read on a production host.
"""

from __future__ import annotations

import csv
import io
import logging
import os
import uuid
from pathlib import Path
from typing import Any

from aakaar.interpreter.activities.registry import ActivityRegistry
from aakaar.interpreter.activities.types import ActivityContext
from aakaar.storage.object_store import parse_uri

logger = logging.getLogger(__name__)


async def parse_csv(ctx: ActivityContext, inputs: dict[str, Any]) -> dict[str, Any]:
    file_uri = inputs["file_uri"]
    delimiter = inputs.get("delimiter", ",")
    has_header = bool(inputs.get("has_header", True))

    data = ctx.object_store.get(file_uri)
    text = data.decode("utf-8")
    if has_header:
        rows = [dict(r) for r in csv.DictReader(io.StringIO(text), delimiter=delimiter)]
    else:
        rows = [
            {f"c{i}": v for i, v in enumerate(row)}
            for row in csv.reader(io.StringIO(text), delimiter=delimiter)
        ]
    logger.debug(
        "file.parse_csv uri=%s rows=%d delimiter=%r has_header=%s",
        file_uri,
        len(rows),
        delimiter,
        has_header,
    )
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
    logger.info("file.write_csv uri=%s rows=%d cols=%d", file_uri, len(rows), len(fieldnames))
    return {"file_uri": file_uri}


async def read_local(ctx: ActivityContext, inputs: dict[str, Any]) -> dict[str, Any]:
    """Copy a local file into managed storage and return its `aakaar://` URI.

    Use this only when a workflow needs to upload a file the user already
    has on their machine (e.g. a CSV from Downloads). Disabled by default
    — set AAKAAR_ALLOW_LOCAL_PATHS=true to enable on dev hosts where the
    API and the user share a filesystem.
    """
    if os.environ.get("AAKAAR_ALLOW_LOCAL_PATHS", "false").lower() not in (
        "1",
        "true",
        "yes",
    ):
        logger.warning(
            "file.read_local denied: AAKAAR_ALLOW_LOCAL_PATHS not enabled (run_id=%s)", ctx.run_id
        )
        raise PermissionError(
            "file.read_local is disabled; set AAKAAR_ALLOW_LOCAL_PATHS=true on "
            "the API host to allow ingesting local files into managed storage"
        )

    raw_path = str(inputs["path"]).strip()
    if not raw_path:
        raise ValueError("path must be non-empty")

    # Expand ~ then resolve against /. `resolve(strict=True)` would also
    # collapse `..` and confirm existence in one shot, but we want a
    # clearer error if the file is just missing vs. if it's traversal-y.
    src = Path(raw_path).expanduser()
    if not src.is_absolute():
        raise ValueError(f"path must be absolute, got {raw_path!r}")
    src = src.resolve()
    if not src.is_file():
        raise FileNotFoundError(f"no such file: {raw_path}")

    data = src.read_bytes()
    key = f"runs/{ctx.run_id}/local-uploads/{uuid.uuid4().hex}_{src.name}"
    obj = ctx.object_store.put(str(ctx.tenant_id), key, data)
    logger.info(
        "file.read_local ingested src=%s uri=%s size=%d run_id=%s",
        src,
        obj.uri,
        len(data),
        ctx.run_id,
    )
    return {"file_uri": obj.uri, "filename": src.name, "size": len(data)}


def register_into(reg: ActivityRegistry) -> None:
    reg.register("file.parse_csv", parse_csv)
    reg.register("file.write_csv", write_csv)
    reg.register("file.read_local", read_local)
