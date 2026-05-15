"""cap.sftp_read — pull a file off an SFTP server into managed storage.

Reads a single remote file via the authenticated SFTP session and
writes the bytes to the tenant's object store under a per-run key, so
downstream nodes can consume it with the same `aakar://` URI the rest
of the platform speaks.

The capability does NOT log in. It expects a `session` produced by an
upstream `cap.sftp_login`.

Size guard:
  - `max_bytes` (default 100 MiB) caps the file. SFTP servers can host
    multi-GB exports; without a cap a planner mistake could OOM the
    worker or fill the tenant's storage quota. Exceeding the cap is a
    node failure, not a silent truncate.

Streaming:
  - We pull the file in chunks via `sftp.open(...).read()` rather than
    asyncssh's `get()` helper, because `get()` writes to a local path
    and we want the bytes in memory to hand to ObjectStorage.put. For
    files larger than ~100 MB this should be revisited (stream into a
    spooled tempfile, then `put_file` it) but v1's cap keeps us well
    inside the in-memory regime.
"""

from __future__ import annotations

import logging
import uuid
from pathlib import PurePosixPath
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from aakar.capabilities._sftp_session import get_holder, normalize_remote_path
from aakar.interpreter.activities.types import ActivityContext
from aakar.shared.registry import CapabilityDefinition

logger = logging.getLogger(__name__)
CAP_REF = "cap.sftp_read"

_DEFAULT_MAX_BYTES = 100 * 1024 * 1024  # 100 MiB
_READ_CHUNK = 256 * 1024


class _Inputs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session: str = Field(
        description="SFTP session handle from cap.sftp_login, e.g. ${login.session}."
    )
    remote_path: str = Field(description="Absolute remote path of the file to read.")
    max_bytes: int = Field(
        default=_DEFAULT_MAX_BYTES,
        ge=1,
        le=2 * 1024 * 1024 * 1024,
        description=(
            "Hard cap on bytes read. Exceeding the cap fails the node; "
            "default 100 MiB."
        ),
    )


class _Outputs(BaseModel):
    uri: str = Field(description="Managed-storage URI (aakar://...) of the stored file.")
    filename: str = Field(description="Basename of `remote_path`, for downstream UX.")
    size: int = Field(description="Bytes written.")


definition = CapabilityDefinition(
    ref=CAP_REF,
    description=(
        "Read a remote file over an authenticated SFTP session and store it "
        "in managed storage. Returns the storage URI."
    ),
    input_schema=_Inputs,
    output_schema=_Outputs,
    secrets=(),
    tags=("sftp", "download"),
)


async def handler(ctx: ActivityContext, inputs: dict[str, Any]) -> dict[str, Any]:
    holder = get_holder(ctx, inputs["session"])
    remote = normalize_remote_path(inputs["remote_path"])
    max_bytes = int(inputs.get("max_bytes", _DEFAULT_MAX_BYTES))

    # Pre-check size when the server reports it. Avoids streaming a 5 GB
    # file just to hit the cap and bail out — we know up front it won't
    # fit. Some servers don't return size; in that case we fall through
    # to the streaming guard below.
    try:
        attrs = await holder.sftp.stat(remote)
    except Exception as e:
        raise RuntimeError(
            f"cap.sftp_read: cannot stat {remote!r} on {holder.host}: {e}"
        ) from e
    reported_size = getattr(attrs, "size", None)
    if isinstance(reported_size, int) and reported_size > max_bytes:
        raise RuntimeError(
            f"cap.sftp_read: {remote!r} is {reported_size} bytes > "
            f"max_bytes={max_bytes}; raise the cap or read a smaller file"
        )

    logger.info(
        "cap.sftp_read start session=%s host=%s remote=%s reported_size=%s",
        inputs["session"],
        holder.host,
        remote,
        reported_size,
    )

    buf = bytearray()
    async with holder.sftp.open(remote, "rb") as fh:
        while True:
            chunk = await fh.read(_READ_CHUNK)
            if not chunk:
                break
            if len(buf) + len(chunk) > max_bytes:
                raise RuntimeError(
                    f"cap.sftp_read: {remote!r} exceeded max_bytes={max_bytes} "
                    f"mid-stream (server did not report size up front)"
                )
            buf.extend(chunk)

    filename = PurePosixPath(remote).name or "download.bin"
    key = f"runs/{ctx.run_id}/sftp/{uuid.uuid4().hex}_{filename}"
    obj = ctx.object_store.put(str(ctx.tenant_id), key, bytes(buf))
    logger.info(
        "cap.sftp_read ok session=%s remote=%s uri=%s bytes=%d",
        inputs["session"],
        remote,
        obj.uri,
        len(buf),
    )
    return {"uri": obj.uri, "filename": filename, "size": len(buf)}
