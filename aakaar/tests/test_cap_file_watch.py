"""Tests for cap.file_watch.

Drives the handler with a hand-built ActivityContext + a tmp LocalFs object
store. The watch is bounded, so every test is fast and deterministic:
  - background put during the watch window -> detects a 'create'
  - in-place overwrite (same size) -> detects a 'modify' via sha256
  - delete during the window -> detects a 'delete'
  - no change -> times out with an empty change set
  - input validation + pure helpers (_fingerprint, _diff)
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

import pytest
from pydantic import ValidationError

from aakaar.capabilities.files.file_watch import (
    CAP_REF,
    _diff,
    _fingerprint,
    definition,
    handler,
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


def _put(ctx: ActivityContext, key: str, data: bytes) -> str:
    return ctx.object_store.put(str(ctx.tenant_id), key, data).uri


# --------------------------------------------------------------------------
# Handler — happy paths
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_detects_background_create(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    # Seed an unrelated baseline object so the diff isn't trivially "first file".
    _put(ctx, "inbox/existing.txt", b"hello")

    async def writer() -> None:
        await asyncio.sleep(0.15)
        _put(ctx, "inbox/new.csv", b"a,b\n1,2\n")

    task = asyncio.create_task(writer())
    out = await handler(ctx, {"prefix": "inbox/", "timeout_s": 3.0, "poll_ms": 50})
    await task

    assert out["changed"] is True
    assert out["changes"] == [{"key": "inbox/new.csv", "kind": "create"}]
    assert out["polls"] >= 1
    assert out["elapsed_s"] < 3.0


@pytest.mark.asyncio
async def test_detects_modify_same_size(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    _put(ctx, "data/report.txt", b"AAAA")  # 4 bytes

    async def writer() -> None:
        await asyncio.sleep(0.1)
        _put(ctx, "data/report.txt", b"BBBB")  # same size, different content

    task = asyncio.create_task(writer())
    out = await handler(ctx, {"prefix": "data/", "timeout_s": 3.0, "poll_ms": 50})
    await task

    assert out["changed"] is True
    assert out["changes"] == [{"key": "data/report.txt", "kind": "modify"}]


@pytest.mark.asyncio
async def test_detects_delete(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    uri = _put(ctx, "tmp/gone.bin", b"\x00\x01\x02")

    async def deleter() -> None:
        await asyncio.sleep(0.1)
        ctx.object_store.delete(uri)

    task = asyncio.create_task(deleter())
    out = await handler(ctx, {"prefix": "tmp/", "timeout_s": 3.0, "poll_ms": 50})
    await task

    assert out["changed"] is True
    assert out["changes"] == [{"key": "tmp/gone.bin", "kind": "delete"}]


@pytest.mark.asyncio
async def test_timeout_no_change(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    _put(ctx, "stable/file.txt", b"unchanging")

    out = await handler(ctx, {"prefix": "stable/", "timeout_s": 0.3, "poll_ms": 50})

    assert out["changed"] is False
    assert out["changes"] == []
    assert out["polls"] >= 1
    # Bounded: never blocks past the timeout (allow scheduler slack).
    assert out["elapsed_s"] < 1.0


@pytest.mark.asyncio
async def test_prefix_isolation(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    # Watch 'inbox/' but the only change happens elsewhere -> timeout, no change.
    async def writer() -> None:
        await asyncio.sleep(0.05)
        _put(ctx, "outbox/other.txt", b"x")

    task = asyncio.create_task(writer())
    out = await handler(ctx, {"prefix": "inbox/", "timeout_s": 0.3, "poll_ms": 50})
    await task

    assert out["changed"] is False
    assert out["changes"] == []


# --------------------------------------------------------------------------
# Input validation + definition shape
# --------------------------------------------------------------------------


def test_definition_shape() -> None:
    assert definition.ref == CAP_REF == "cap.file_watch"
    assert definition.secrets == ()
    assert "watch" in definition.tags
    with pytest.raises(ValidationError):
        definition.input_schema(prefix="inbox/", bogus=1)


def test_input_schema_rejects_bad_values() -> None:
    with pytest.raises(ValidationError):
        definition.input_schema(timeout_s=0)  # must be > 0
    with pytest.raises(ValidationError):
        definition.input_schema(poll_ms=1)  # below floor


def test_input_schema_defaults() -> None:
    m = definition.input_schema()
    assert m.prefix == ""
    assert m.timeout_s == 10.0
    assert m.poll_ms == 500


# --------------------------------------------------------------------------
# Pure helpers
# --------------------------------------------------------------------------


def test_fingerprint_and_diff(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    _put(ctx, "p/a.txt", b"one")
    _put(ctx, "p/b.txt", b"two")
    _put(ctx, "q/c.txt", b"three")  # outside prefix

    base = _fingerprint(ctx.object_store, str(ctx.tenant_id), "p/")
    assert set(base) == {"p/a.txt", "p/b.txt"}
    assert all(v for v in base.values())  # non-empty fingerprints

    # No change.
    assert _diff(base, dict(base)) == []

    # Modify a.txt, create d.txt, delete b.txt.
    cur = dict(base)
    cur["p/a.txt"] = "changed-digest"
    cur["p/d.txt"] = "new-digest"
    del cur["p/b.txt"]
    assert _diff(base, cur) == [
        {"key": "p/a.txt", "kind": "modify"},
        {"key": "p/b.txt", "kind": "delete"},
        {"key": "p/d.txt", "kind": "create"},
    ]
