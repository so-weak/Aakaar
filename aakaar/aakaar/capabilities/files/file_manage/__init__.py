"""cap.file_manage — object-store file operations.

A thin, deterministic wrapper over `ctx.object_store` for the housekeeping
operations a DAG needs between steps: copy, move, delete, existence checks,
stat, and prefix listing. This is NOT arbitrary local-filesystem access —
every path is a tenant-scoped object key or an `aakaar://t/{tenant}/{key}`
URI, and all I/O goes through the object store so tenant isolation and the
eventual S3 swap are preserved.

Inputs:
  op:     one of copy | move | delete | exists | stat | list.
  src:    object reference — either a full `aakaar://` URI or a bare key
          (resolved against the run's tenant). Required for every op
          except `list`.
  dst:    destination reference (URI or key). Required for copy/move.
  prefix: key prefix to filter a `list` (optional; lists everything when
          omitted). Ignored by the other ops.

The object store exposes no native copy/move, so those are implemented as
get + put (+ delete for move). `exists` never raises on a miss — it returns
`{"exists": false}`. The other ops surface `ObjectNotFound` as a clear
RuntimeError so a failed node has a readable message.

Output (op-dependent):
  copy:   {op, src, dst, size, sha256}
  move:   {op, src, dst, size, sha256}
  delete: {op, src, deleted}
  exists: {op, src, exists}
  stat:   {op, src, exists, size, sha256, key, tenant_id}
  list:   {op, prefix, count, objects: [{uri, key, size}, ...]}
"""

from __future__ import annotations

import contextlib
import logging
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from aakaar.interpreter.activities.types import ActivityContext
from aakaar.shared.registry import CapabilityDefinition
from aakaar.storage.object_store import (
    URI_PREFIX,
    ObjectNotFound,
    make_uri,
    parse_uri,
)

logger = logging.getLogger(__name__)
CAP_REF = "cap.file_manage"

_Op = Literal["copy", "move", "delete", "exists", "stat", "list"]


class _Inputs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    op: _Op = Field(
        description=(
            "Object-store operation: copy, move, delete, exists, stat, or list."
        )
    )
    src: str | None = Field(
        default=None,
        description=(
            "Source object reference: a full 'aakaar://t/{tenant}/{key}' URI "
            "or a bare key resolved against the run's tenant. Required for "
            "every op except 'list'."
        ),
    )
    dst: str | None = Field(
        default=None,
        description=(
            "Destination object reference (URI or key). Required for "
            "'copy' and 'move'."
        ),
    )
    prefix: str | None = Field(
        default=None,
        description=(
            "Key prefix to filter a 'list'. Lists every tenant object when "
            "omitted. Ignored by the other ops."
        ),
    )


class _ListedObject(BaseModel):
    uri: str
    key: str
    size: int


class _Outputs(BaseModel):
    op: str = Field(description="The operation that ran.")
    src: str | None = Field(default=None, description="Resolved source URI, when applicable.")
    dst: str | None = Field(default=None, description="Resolved destination URI, for copy/move.")
    exists: bool | None = Field(default=None, description="Set by 'exists' and 'stat'.")
    deleted: bool | None = Field(default=None, description="Set by 'delete'.")
    size: int | None = Field(default=None, description="Byte size, for copy/move/stat.")
    sha256: str | None = Field(default=None, description="Content digest, for copy/move/stat.")
    key: str | None = Field(default=None, description="Tenant-relative key, for 'stat'.")
    tenant_id: str | None = Field(default=None, description="Owning tenant, for 'stat'.")
    prefix: str | None = Field(default=None, description="Effective prefix, for 'list'.")
    count: int | None = Field(default=None, description="Number of objects, for 'list'.")
    objects: list[_ListedObject] | None = Field(
        default=None, description="Listed objects, for 'list'."
    )


definition = CapabilityDefinition(
    ref=CAP_REF,
    description=(
        "Object-store file housekeeping: copy, move, delete, exists, stat, "
        "and prefix-list of tenant-scoped objects. Operates only on the "
        "managed object store (aakaar:// URIs or bare keys) — never the "
        "local filesystem."
    ),
    input_schema=_Inputs,
    output_schema=_Outputs,
    secrets=(),
    tags=("files", "storage", "object-store"),
)


def resolve_uri(tenant_id: str, ref: str) -> str:
    """Normalize a src/dst reference to a canonical aakaar:// URI.

    Accepts either a full URI (validated/normalized via parse_uri) or a bare
    tenant-relative key. Raises ValueError on a malformed reference.
    """
    ref = (ref or "").strip()
    if not ref:
        raise ValueError("empty object reference")
    if ref.startswith(URI_PREFIX):
        tid, key = parse_uri(ref)
        return make_uri(tid, key)
    # Bare key: scope to the run's tenant.
    return make_uri(tenant_id, ref)


def _require(inputs: dict[str, Any], name: str, op: str) -> str:
    val = inputs.get(name)
    if not val or not str(val).strip():
        raise RuntimeError(f"cap.file_manage: op {op!r} requires `{name}`")
    return str(val)


async def handler(ctx: ActivityContext, inputs: dict[str, Any]) -> dict[str, Any]:
    op = inputs["op"]
    tenant = str(ctx.tenant_id)
    store = ctx.object_store

    if op == "list":
        prefix = (inputs.get("prefix") or "").strip()
        objs = store.list(tenant, prefix=prefix)
        listed = [
            {"uri": o.uri, "key": o.key, "size": o.size} for o in objs
        ]
        logger.info(
            "cap.file_manage list run_id=%s prefix=%r count=%d",
            ctx.run_id,
            prefix,
            len(listed),
        )
        return {
            "op": op,
            "prefix": prefix,
            "count": len(listed),
            "objects": listed,
        }

    # All remaining ops need a source.
    try:
        src_uri = resolve_uri(tenant, _require(inputs, "src", op))
    except ValueError as e:
        raise RuntimeError(f"cap.file_manage: bad `src`: {e}") from e

    if op == "exists":
        try:
            store.stat(src_uri)
            exists = True
        except ObjectNotFound:
            exists = False
        logger.info(
            "cap.file_manage exists run_id=%s src=%s -> %s",
            ctx.run_id,
            src_uri,
            exists,
        )
        return {"op": op, "src": src_uri, "exists": exists}

    if op == "stat":
        try:
            obj = store.stat(src_uri)
        except ObjectNotFound as e:
            raise RuntimeError(
                f"cap.file_manage: stat target not found: {src_uri}"
            ) from e
        logger.info(
            "cap.file_manage stat run_id=%s src=%s size=%d",
            ctx.run_id,
            src_uri,
            obj.size,
        )
        return {
            "op": op,
            "src": src_uri,
            "exists": True,
            "size": obj.size,
            "sha256": obj.sha256,
            "key": obj.key,
            "tenant_id": obj.tenant_id,
        }

    if op == "delete":
        try:
            store.delete(src_uri)
            deleted = True
        except ObjectNotFound:
            # Idempotent: deleting a missing object is a no-op, not a failure.
            deleted = False
        logger.info(
            "cap.file_manage delete run_id=%s src=%s deleted=%s",
            ctx.run_id,
            src_uri,
            deleted,
        )
        return {"op": op, "src": src_uri, "deleted": deleted}

    # copy / move both need a destination and read the source bytes.
    try:
        dst_uri = resolve_uri(tenant, _require(inputs, "dst", op))
    except ValueError as e:
        raise RuntimeError(f"cap.file_manage: bad `dst`: {e}") from e

    if dst_uri == src_uri:
        raise RuntimeError(
            f"cap.file_manage: op {op!r} src and dst resolve to the same "
            f"object: {src_uri}"
        )

    try:
        data = store.get(src_uri)
    except ObjectNotFound as e:
        raise RuntimeError(
            f"cap.file_manage: {op} source not found: {src_uri}"
        ) from e

    dst_tenant, dst_key = parse_uri(dst_uri)
    stored = store.put(dst_tenant, dst_key, data)

    if op == "move":
        # Source bytes are safely written to dst; remove the original.
        with contextlib.suppress(ObjectNotFound):
            store.delete(src_uri)

    logger.info(
        "cap.file_manage %s run_id=%s src=%s dst=%s size=%d",
        op,
        ctx.run_id,
        src_uri,
        dst_uri,
        stored.size,
    )
    return {
        "op": op,
        "src": src_uri,
        "dst": stored.uri,
        "size": stored.size,
        "sha256": stored.sha256,
    }
