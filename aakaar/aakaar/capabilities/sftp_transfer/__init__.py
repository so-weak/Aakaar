"""cap.sftp_transfer — move/copy a file between two paths on SFTP.

Two flavors, picked by the `mode` input:

  - `mode='rename'` (the default and the cheap path): uses SFTP's
    `rename` operation. Atomic on the same filesystem, fails fast if
    crossing devices or if the destination exists (when overwrite is
    off).
  - `mode='copy'`: streams the source through the worker — reads bytes
    via the SFTP session, writes them back to the destination path on
    the same session. There's no server-side cp in SFTP; this is the
    honest cost. Same session, same auth — the bytes still traverse
    the worker.

When you want to move bytes between two *different* SFTP servers, the
intended composition is `cap.sftp_read` (server A → managed storage) →
`cap.sftp_write` (managed storage → server B). That keeps each
capability single-session and the planner doesn't have to juggle two
auth grants in one node.

The capability does NOT log in. It expects a `session` produced by an
upstream `cap.sftp_login`.
"""

from __future__ import annotations

import logging
from pathlib import PurePosixPath
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from aakaar.capabilities._sftp_session import get_holder, normalize_remote_path
from aakaar.interpreter.activities.types import ActivityContext
from aakaar.shared.registry import CapabilityDefinition

logger = logging.getLogger(__name__)
CAP_REF = "cap.sftp_transfer"

_CHUNK = 256 * 1024


class _Inputs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session: str = Field(
        description="SFTP session handle from cap.sftp_login, e.g. ${login.session}."
    )
    src_path: str = Field(description="Absolute source remote path.")
    dst_path: str = Field(
        description=(
            "Absolute destination remote path. If it ends with '/', the "
            "source basename is appended."
        ),
    )
    mode: str = Field(
        default="rename",
        description=(
            "'rename' (atomic same-filesystem move) or 'copy' (stream "
            "through the worker; source is left in place)."
        ),
    )
    overwrite: bool = Field(
        default=False,
        description="Overwrite the destination if it exists. Default false.",
    )
    make_parents: bool = Field(
        default=False,
        description="Create missing parent directories on the destination side.",
    )


class _Outputs(BaseModel):
    src_path: str = Field(description="Echo of the source path.")
    dst_path: str = Field(description="Final destination path after any normalization.")
    mode: str = Field(description="Mode actually used ('rename' or 'copy').")


definition = CapabilityDefinition(
    ref=CAP_REF,
    description=(
        "Transfer a file between two paths on the same SFTP server, either by "
        "atomic rename or by streaming a copy. To move between two different "
        "servers, compose cap.sftp_read + cap.sftp_write."
    ),
    input_schema=_Inputs,
    output_schema=_Outputs,
    secrets=(),
    tags=("sftp", "transfer"),
)


async def handler(ctx: ActivityContext, inputs: dict[str, Any]) -> dict[str, Any]:
    holder = get_holder(ctx, inputs["session"])
    src = normalize_remote_path(inputs["src_path"])
    dst = normalize_remote_path(inputs["dst_path"])
    mode = inputs.get("mode") or "rename"
    overwrite = bool(inputs.get("overwrite", False))
    make_parents = bool(inputs.get("make_parents", False))

    if mode not in ("rename", "copy"):
        raise ValueError(
            f"cap.sftp_transfer: mode must be 'rename' or 'copy', got {mode!r}"
        )

    if dst.endswith("/"):
        dst = dst + PurePosixPath(src).name

    if not overwrite:
        try:
            await holder.sftp.stat(dst)
        except Exception:
            pass
        else:
            raise RuntimeError(
                f"cap.sftp_transfer: destination {dst!r} already exists on "
                f"{holder.host} and overwrite=false"
            )

    if make_parents:
        parent = str(PurePosixPath(dst).parent)
        if parent and parent not in ("", "/", "."):
            try:
                await holder.sftp.makedirs(parent, exist_ok=True)
            except Exception as e:
                raise RuntimeError(
                    f"cap.sftp_transfer: could not create parent dir "
                    f"{parent!r} on {holder.host}: {e}"
                ) from e

    logger.info(
        "cap.sftp_transfer start session=%s host=%s mode=%s src=%s dst=%s",
        inputs["session"],
        holder.host,
        mode,
        src,
        dst,
    )

    if mode == "rename":
        # asyncssh exposes `rename` (POSIX) and `posix_rename` (SFTPv4+
        # extension, which behaves like POSIX rename including overwrite
        # semantics on supporting servers). We try posix_rename first
        # when overwrite is on — only on the overwrite path because the
        # plain `rename` already fails-fast on existing targets, which
        # is what we want when overwrite is off.
        try:
            if overwrite and hasattr(holder.sftp, "posix_rename"):
                await holder.sftp.posix_rename(src, dst)
            else:
                await holder.sftp.rename(src, dst)
        except Exception as e:
            # SFTP servers may reject cross-filesystem rename with
            # SSH_FX_OP_UNSUPPORTED or SSH_FX_FAILURE. Surface the
            # mode='copy' hint so the planner can retry without
            # guessing.
            raise RuntimeError(
                f"cap.sftp_transfer: rename {src!r}→{dst!r} failed on "
                f"{holder.host}: {e}. If the paths are on different "
                f"filesystems, retry with mode='copy'."
            ) from e
    else:
        # Copy: stream through the worker. We don't try to be clever
        # with server-side hardlinks — most SFTP servers don't expose
        # them, and the bytes-through-worker cost is what the user
        # asked for by picking this mode.
        async with holder.sftp.open(src, "rb") as rfh, holder.sftp.open(dst, "wb") as wfh:
            while True:
                chunk = await rfh.read(_CHUNK)
                if not chunk:
                    break
                await wfh.write(chunk)

    logger.info(
        "cap.sftp_transfer ok session=%s mode=%s src=%s dst=%s",
        inputs["session"],
        mode,
        src,
        dst,
    )
    return {"src_path": src, "dst_path": dst, "mode": mode}
