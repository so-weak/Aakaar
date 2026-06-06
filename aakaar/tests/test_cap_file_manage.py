"""Tests for cap.file_manage.

Drives the handler with a hand-built ActivityContext + a tmp LocalFs object
store, covering each op (copy, move, delete, exists, stat, list), bare-key
vs full-URI references, idempotent delete, validation errors, and the pure
resolve_uri helper / definition shape.
"""

from __future__ import annotations

import hashlib
import uuid
from pathlib import Path

import pytest
from pydantic import ValidationError

from aakaar.capabilities.files.file_manage import (
    CAP_REF,
    definition,
    handler,
    resolve_uri,
)
from aakaar.interpreter.activities.types import ActivityContext


def _ctx(tmp_path: Path) -> ActivityContext:
    from aakaar.shared.registry import build_default_registry
    from aakaar.storage import LocalFsObjectStore
    from aakaar.vault import LocalVault

    return ActivityContext(
        tenant_id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        registry=build_default_registry(),
        object_store=LocalFsObjectStore(tmp_path / "objs"),
        vault=LocalVault(tmp_path / "vault"),
    )


def _seed(ctx: ActivityContext, key: str, data: bytes) -> str:
    return ctx.object_store.put(str(ctx.tenant_id), key, data).uri


# --------------------------------------------------------------------------
# Happy paths
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_copy_then_stat_and_list(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    payload = b"hello-object-store"
    src = _seed(ctx, "in/a.txt", payload)

    out = await handler(ctx, {"op": "copy", "src": src, "dst": "out/b.txt"})
    assert out["op"] == "copy"
    assert out["src"] == src
    assert out["dst"].endswith("/out/b.txt")
    assert out["size"] == len(payload)
    assert out["sha256"] == hashlib.sha256(payload).hexdigest()

    # Original still present after a copy.
    assert ctx.object_store.get(src) == payload
    # Destination has the same bytes.
    assert ctx.object_store.get(out["dst"]) == payload

    stat = await handler(ctx, {"op": "stat", "src": out["dst"]})
    assert stat["exists"] is True
    assert stat["size"] == len(payload)
    assert stat["key"] == "out/b.txt"
    assert stat["tenant_id"] == str(ctx.tenant_id)

    listing = await handler(ctx, {"op": "list", "prefix": "out/"})
    assert listing["count"] == 1
    assert listing["objects"][0]["key"] == "out/b.txt"
    assert listing["objects"][0]["size"] == len(payload)

    listing_all = await handler(ctx, {"op": "list"})
    keys = {o["key"] for o in listing_all["objects"]}
    assert keys == {"in/a.txt", "out/b.txt"}


@pytest.mark.asyncio
async def test_move_removes_source(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    payload = b"move-me"
    src = _seed(ctx, "stage/x.bin", payload)

    out = await handler(ctx, {"op": "move", "src": "stage/x.bin", "dst": "final/x.bin"})
    assert out["op"] == "move"
    assert out["dst"].endswith("/final/x.bin")
    # Destination present, source gone.
    assert ctx.object_store.get(out["dst"]) == payload
    exists_src = await handler(ctx, {"op": "exists", "src": src})
    assert exists_src["exists"] is False


@pytest.mark.asyncio
async def test_exists_true_false(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    _seed(ctx, "doc.pdf", b"x")
    hit = await handler(ctx, {"op": "exists", "src": "doc.pdf"})
    assert hit["exists"] is True
    miss = await handler(ctx, {"op": "exists", "src": "nope.pdf"})
    assert miss["exists"] is False


@pytest.mark.asyncio
async def test_delete_idempotent(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    _seed(ctx, "tmp/y.txt", b"data")
    first = await handler(ctx, {"op": "delete", "src": "tmp/y.txt"})
    assert first["deleted"] is True
    second = await handler(ctx, {"op": "delete", "src": "tmp/y.txt"})
    assert second["deleted"] is False
    assert (await handler(ctx, {"op": "exists", "src": "tmp/y.txt"}))["exists"] is False


@pytest.mark.asyncio
async def test_list_empty_tenant(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    out = await handler(ctx, {"op": "list"})
    assert out["count"] == 0
    assert out["objects"] == []


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_copy_missing_src_raises(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    with pytest.raises(RuntimeError, match="source not found"):
        await handler(ctx, {"op": "copy", "src": "ghost.txt", "dst": "out.txt"})


@pytest.mark.asyncio
async def test_stat_missing_raises(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    with pytest.raises(RuntimeError, match="not found"):
        await handler(ctx, {"op": "stat", "src": "ghost.txt"})


@pytest.mark.asyncio
async def test_copy_requires_dst(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    _seed(ctx, "a.txt", b"z")
    with pytest.raises(RuntimeError, match="requires `dst`"):
        await handler(ctx, {"op": "copy", "src": "a.txt"})


@pytest.mark.asyncio
async def test_op_requires_src(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    with pytest.raises(RuntimeError, match="requires `src`"):
        await handler(ctx, {"op": "stat"})


@pytest.mark.asyncio
async def test_copy_same_src_dst_raises(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    src = _seed(ctx, "a.txt", b"z")
    with pytest.raises(RuntimeError, match="same"):
        await handler(ctx, {"op": "copy", "src": src, "dst": "a.txt"})


# --------------------------------------------------------------------------
# Definition + pure helpers
# --------------------------------------------------------------------------


def test_definition_shape() -> None:
    assert definition.ref == CAP_REF == "cap.file_manage"
    assert definition.secrets == ()
    assert "files" in definition.tags
    with pytest.raises(ValidationError):
        definition.input_schema(op="list", bogus=1)
    with pytest.raises(ValidationError):
        definition.input_schema(op="frobnicate")


def test_resolve_uri_bare_key_and_full_uri() -> None:
    tenant = uuid.uuid4().hex
    bare = resolve_uri(tenant, "a/b/c.txt")
    assert bare == f"aakaar://t/{tenant}/a/b/c.txt"
    # Round-trips a full URI (preserving its own tenant).
    full = f"aakaar://t/{tenant}/x.txt"
    assert resolve_uri("other-tenant", full) == full


def test_resolve_uri_rejects_empty() -> None:
    with pytest.raises(ValueError):
        resolve_uri("t", "")
