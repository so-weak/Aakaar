"""cap.sftp_write — push a file from managed storage onto an SFTP server.

Reads the bytes for an `aakaar://` URI from the tenant's object store
and writes them to the supplied remote path via the authenticated SFTP
session.

The capability does NOT log in. It expects a `session` produced by an
upstream `cap.sftp_login`.

Path handling:
  - `remote_path` is taken verbatim — relative paths resolve against
    the server-side default directory (usually the user's home). The
    planner should pass absolute paths when it cares which directory
    the file lands in.
  - `make_parents`: when true, missing intermediate directories are
    created with `sftp.makedirs`. Off by default — silently creating
    a directory tree on a third-party server is the kind of side
    effect the planner shouldn't trigger without an explicit ask.
  - `overwrite`: when false (default), pre-checks `stat()` and refuses
    to write if the target already exists. SFTP's atomic open-without-
    truncate flags vary by server, so we do an explicit pre-check
    rather than trying to translate POSIX open flags.
"""

from __future__ import annotations

import logging
import re
from pathlib import PurePosixPath
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from aakaar.capabilities._sftp_session import get_holder, normalize_remote_path
from aakaar.interpreter.activities.types import ActivityContext
from aakaar.shared.registry import CapabilityDefinition

logger = logging.getLogger(__name__)
CAP_REF = "cap.sftp_write"

_WRITE_CHUNK = 256 * 1024


class _Inputs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session: str = Field(
        description="SFTP session handle from cap.sftp_login, e.g. ${login.session}."
    )
    file_uri: str = Field(
        description=(
            "Managed-storage URI (aakaar://...) of the source file. Use upstream "
            "`${node.uri}` references; do not embed literal paths."
        ),
    )
    remote_path: str = Field(
        description=(
            "Absolute remote path to write to. If `remote_path` ends in '/', "
            "the basename embedded in `file_uri` is appended."
        ),
    )
    make_parents: bool = Field(
        default=False,
        description=(
            "Create missing parent directories on the server. Default false."
        ),
    )
    overwrite: bool = Field(
        default=False,
        description=(
            "Overwrite the target if it already exists. Default false — "
            "writing fails fast when the path is occupied."
        ),
    )


class _Outputs(BaseModel):
    remote_path: str = Field(description="Final absolute remote path that was written.")
    size: int = Field(description="Bytes written.")


definition = CapabilityDefinition(
    ref=CAP_REF,
    description=(
        "Write a file from managed storage to an SFTP server over an "
        "authenticated session. Optionally creates parent directories and "
        "refuses to clobber existing files unless overwrite=true."
    ),
    input_schema=_Inputs,
    output_schema=_Outputs,
    secrets=(),
    tags=("sftp", "upload"),
)


# Mirrors aakaar/capabilities/file_upload — recover the user-facing
# basename from a managed-storage key shaped like `<uuid32hex>_<name>`,
# so a put-then-fetch round-trip preserves the original filename.
_STORED_NAME_RE = re.compile(r"^[0-9a-fA-F]{32}_(.+)$")


def _user_facing_basename(file_uri: str) -> str:
    base = file_uri.rsplit("/", 1)[-1] if "/" in file_uri else file_uri
    if not base:
        return "upload.bin"
    m = _STORED_NAME_RE.match(base)
    if m:
        return m.group(1)
    return base


async def handler(ctx: ActivityContext, inputs: dict[str, Any]) -> dict[str, Any]:
    holder = get_holder(ctx, inputs["session"])
    file_uri = inputs["file_uri"]
    if not file_uri.startswith("aakaar://"):
        raise ValueError(
            f"cap.sftp_write: file_uri must start with 'aakaar://', got {file_uri!r}"
        )
    remote = normalize_remote_path(inputs["remote_path"])
    make_parents = bool(inputs.get("make_parents", False))
    overwrite = bool(inputs.get("overwrite", False))

    # If the caller passed a directory (trailing slash), pick a target
    # filename from the source URI so we don't write the whole upload
    # *into* the directory inode (which fails on every SFTP server).
    if remote.endswith("/"):
        remote = remote + _user_facing_basename(file_uri)

    data = ctx.object_store.get(file_uri)

    if not overwrite:
        try:
            await holder.sftp.stat(remote)
        except Exception:
            # stat failure is the happy path here — target doesn't exist.
            pass
        else:
            raise RuntimeError(
                f"cap.sftp_write: {remote!r} already exists on {holder.host} "
                f"and overwrite=false"
            )

    if make_parents:
        parent = str(PurePosixPath(remote).parent)
        if parent and parent not in ("", "/", "."):
            try:
                await holder.sftp.makedirs(parent, exist_ok=True)
            except Exception as e:
                raise RuntimeError(
                    f"cap.sftp_write: could not create parent dir {parent!r} "
                    f"on {holder.host}: {e}"
                ) from e

    logger.info(
        "cap.sftp_write start session=%s host=%s remote=%s bytes=%d overwrite=%s",
        inputs["session"],
        holder.host,
        remote,
        len(data),
        overwrite,
    )

    bytes_written = 0
    async with holder.sftp.open(remote, "wb") as fh:
        for i in range(0, len(data), _WRITE_CHUNK):
            chunk = data[i : i + _WRITE_CHUNK]
            await fh.write(chunk)
            bytes_written += len(chunk)

    logger.info(
        "cap.sftp_write ok session=%s remote=%s bytes=%d",
        inputs["session"],
        remote,
        bytes_written,
    )
    return {"remote_path": remote, "size": bytes_written}
