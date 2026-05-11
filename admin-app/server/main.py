"""Tiny FastAPI service for admin-app — recon file upload + history.

Self-contained: its own venv + requirements, runs on port 8001.
Independent of aakar and nbbl. Files land under
`admin-app/server/uploads/{date}/{switch_type}/{cycle}_{ts}_{name}`.

Local run:

    cd admin-app/server
    python3 -m venv .venv
    .venv/bin/pip install -r requirements.txt
    .venv/bin/uvicorn main:app --host 127.0.0.1 --port 8001 --reload

start.sh launches it automatically.
"""

from __future__ import annotations

import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware


# Resolve paths relative to this file so the working directory at
# uvicorn launch doesn't matter.
_SERVER_DIR = Path(__file__).resolve().parent
_UPLOAD_DIR = _SERVER_DIR / "uploads"
_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

# In-memory history. The frontend's history table reads from
# GET /api/recon/uploads which returns this list newest-first. Restart
# clears it; if persistence matters later, swap to SQLite.
_HISTORY: list[dict] = []

_HUMAN_DATE = "%d/%m/%Y"
_HUMAN_DT = "%d/%m/%Y %H:%M:%S"


app = FastAPI(title="admin-app api", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    ],
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=False,
)


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/recon/uploads")
async def upload_recon(
    file: Annotated[UploadFile, File(...)],
    switch_type: Annotated[str, Form(...)],
    cycle: Annotated[str, Form(...)],
    date: Annotated[str, Form(...)],
    skip_recon: Annotated[str, Form()] = "No",
) -> dict:
    """Save a recon file under uploads/{date}/{switch_type}/.

    Form payload mirrors the page: switch_type ∈ {Issuer, Acquirer},
    cycle e.g. C02, date in ISO yyyy-mm-dd. Returns a row the
    frontend appends directly to its history table.
    """
    if switch_type not in ("Issuer", "Acquirer"):
        raise HTTPException(
            status_code=400,
            detail="switch_type must be 'Issuer' or 'Acquirer'",
        )
    if not file.filename:
        raise HTTPException(status_code=400, detail="missing filename")

    # Strip path traversal — keep only the basename.
    safe_name = Path(file.filename).name
    if not safe_name or safe_name in (".", ".."):
        raise HTTPException(status_code=400, detail="invalid filename")

    # Match the frontend's `accept=".csv,.zip"` so curl users get the
    # same constraint as form users.
    suffix = Path(safe_name).suffix.lower()
    if suffix not in (".csv", ".zip"):
        raise HTTPException(
            status_code=415,
            detail=f"unsupported file type {suffix!r}; allowed: .csv, .zip",
        )

    try:
        datetime.strptime(date, "%Y-%m-%d")
    except ValueError as e:
        raise HTTPException(
            status_code=400, detail=f"date must be yyyy-mm-dd, got {date!r}"
        ) from e

    target_dir = _UPLOAD_DIR / date / switch_type
    target_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    saved_name = f"{cycle}_{ts}_{safe_name}"
    target_path = target_dir / saved_name

    # Stream to disk — UploadFile's spooled tempfile keeps RAM bounded.
    with target_path.open("wb") as out:
        shutil.copyfileobj(file.file, out)

    size = target_path.stat().st_size
    now = datetime.now()

    row = {
        "id": str(uuid.uuid4()),
        "fileName": safe_name,
        "switchType": switch_type,
        "cycle": cycle,
        "date": now.strftime(_HUMAN_DATE),
        "skipRecon": skip_recon,
        "status": "Uploaded",
        "uploadedAt": now.strftime(_HUMAN_DT),
        "savedTo": str(target_path.relative_to(_SERVER_DIR)),
        "size": size,
    }
    _HISTORY.insert(0, row)
    return row


@app.get("/api/recon/uploads")
def list_recon_uploads() -> list[dict]:
    """Newest-first history. The frontend table renders these directly."""
    return list(_HISTORY)
