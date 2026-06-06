"""Tests for cap.archive_manage.

Drives the handler with a hand-built ActivityContext and a tmp object store
seeded with two files, covering:
  - create zip -> list -> extract round-trip (the spec's happy path)
  - tar and tar.gz round-trips
  - format sniffing when `format` is omitted on list/extract
  - duplicate-basename disambiguation on create
  - input validation (missing/forbidden fields)
  - path-traversal rejection on extract
  - definition shape and pure helpers
"""

from __future__ import annotations

import io
import tarfile
import uuid
import zipfile
from pathlib import Path

import pytest
from pydantic import ValidationError

from aakaar.capabilities.files.archive_manage import (
    CAP_REF,
    _basename_for_uri,
    _dedupe_names,
    _is_unsafe_member,
    _sniff_format,
    definition,
    handler,
)
from aakaar.interpreter.activities.types import ActivityContext

_A = b"hello from a\n"
_B = b"second file contents\n"


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
# Handler happy path: create -> list -> extract round-trip
# --------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("fmt", ["zip", "tar", "tar.gz"])
async def test_create_list_extract_roundtrip(tmp_path: Path, fmt: str) -> None:
    ctx = _ctx(tmp_path)
    a = _seed(ctx, "in/alpha.txt", _A)
    b = _seed(ctx, "in/beta.txt", _B)

    created = await handler(ctx, {"op": "create", "sources": [a, b], "format": fmt})
    assert created["op"] == "create"
    assert created["format"] == fmt
    assert created["archive_uri"].startswith("aakaar://t/")
    archive_uri = created["archive_uri"]

    # list (format omitted -> sniffed)
    listed = await handler(ctx, {"op": "list", "archive": archive_uri})
    names = {e["name"]: e for e in listed["entries"]}
    assert listed["format"] == fmt
    assert set(names) == {"alpha.txt", "beta.txt"}
    assert names["alpha.txt"]["size"] == len(_A)
    assert names["alpha.txt"]["is_dir"] is False

    # extract (format omitted -> sniffed)
    extracted = await handler(ctx, {"op": "extract", "archive": archive_uri})
    assert extracted["op"] == "extract"
    assert len(extracted["extracted_uris"]) == 2
    by_name = {
        uri.rsplit("/", 1)[-1]: ctx.object_store.get(uri)
        for uri in extracted["extracted_uris"]
    }
    assert by_name["alpha.txt"] == _A
    assert by_name["beta.txt"] == _B


@pytest.mark.asyncio
async def test_create_explicit_format_on_list(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    a = _seed(ctx, "in/alpha.txt", _A)
    created = await handler(ctx, {"op": "create", "sources": [a], "format": "zip"})
    listed = await handler(
        ctx, {"op": "list", "archive": created["archive_uri"], "format": "zip"}
    )
    assert [e["name"] for e in listed["entries"]] == ["alpha.txt"]


@pytest.mark.asyncio
async def test_create_dedupes_duplicate_basenames(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    # Two distinct source objects whose keys share the same basename.
    one = _seed(ctx, "dir1/report.csv", b"one")
    two = _seed(ctx, "dir2/report.csv", b"two")
    created = await handler(
        ctx, {"op": "create", "sources": [one, two], "format": "zip"}
    )
    listed = await handler(ctx, {"op": "list", "archive": created["archive_uri"]})
    names = sorted(e["name"] for e in listed["entries"])
    assert names == ["report (1).csv", "report.csv"]

    extracted = await handler(ctx, {"op": "extract", "archive": created["archive_uri"]})
    contents = sorted(ctx.object_store.get(u) for u in extracted["extracted_uris"])
    assert contents == [b"one", b"two"]


# --------------------------------------------------------------------------
# Security: reject path-traversal members on extract
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_extract_rejects_zip_path_traversal(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("../escape.txt", b"nope")
    evil = _seed(ctx, "in/evil.zip", buf.getvalue())
    with pytest.raises(RuntimeError, match="unsafe archive member"):
        await handler(ctx, {"op": "extract", "archive": evil, "format": "zip"})


@pytest.mark.asyncio
async def test_extract_rejects_tar_symlink(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        info = tarfile.TarInfo(name="link")
        info.type = tarfile.SYMTYPE
        info.linkname = "/etc/passwd"
        tf.addfile(info)
    evil = _seed(ctx, "in/evil.tar", buf.getvalue())
    with pytest.raises(RuntimeError, match="non-regular archive member"):
        await handler(ctx, {"op": "extract", "archive": evil, "format": "tar"})


# --------------------------------------------------------------------------
# Input validation
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_requires_sources(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    with pytest.raises(RuntimeError, match="requires `sources`"):
        await handler(ctx, {"op": "create", "format": "zip"})


@pytest.mark.asyncio
async def test_create_requires_format(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    a = _seed(ctx, "in/alpha.txt", _A)
    with pytest.raises(RuntimeError, match="requires `format`"):
        await handler(ctx, {"op": "create", "sources": [a]})


@pytest.mark.asyncio
async def test_extract_requires_archive(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    with pytest.raises(RuntimeError, match="requires `archive`"):
        await handler(ctx, {"op": "extract"})


@pytest.mark.asyncio
async def test_list_rejects_sources(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    a = _seed(ctx, "in/alpha.txt", _A)
    created = await handler(ctx, {"op": "create", "sources": [a], "format": "zip"})
    with pytest.raises(RuntimeError, match="must not set `sources`"):
        await handler(
            ctx,
            {"op": "list", "archive": created["archive_uri"], "sources": [a]},
        )


# --------------------------------------------------------------------------
# Definition + pure helpers
# --------------------------------------------------------------------------


def test_definition_shape() -> None:
    assert definition.ref == CAP_REF == "cap.archive_manage"
    assert definition.secrets == ()
    assert "archive" in definition.tags
    with pytest.raises(ValidationError):
        definition.input_schema(op="create", bogus=1)
    with pytest.raises(ValidationError):
        definition.input_schema(op="frobnicate")
    with pytest.raises(ValidationError):
        definition.input_schema(op="create", format="rar")


def test_basename_for_uri() -> None:
    assert _basename_for_uri("aakaar://t/x/runs/abc/out/report.csv") == "report.csv"
    assert _basename_for_uri("aakaar://t/x/a.zip") == "a.zip"


def test_dedupe_names() -> None:
    assert _dedupe_names(["a.txt", "a.txt", "b.txt", "a.txt"]) == [
        "a.txt",
        "a (1).txt",
        "b.txt",
        "a (2).txt",
    ]
    assert _dedupe_names(["noext", "noext"]) == ["noext", "noext (1)"]


def test_is_unsafe_member() -> None:
    assert _is_unsafe_member("../x") is True
    assert _is_unsafe_member("/abs") is True
    assert _is_unsafe_member("a/../b") is True
    assert _is_unsafe_member("C:\\win") is True
    assert _is_unsafe_member("") is True
    assert _is_unsafe_member("dir/file.txt") is False
    assert _is_unsafe_member("file.txt") is False


def test_sniff_format() -> None:
    zbuf = io.BytesIO()
    with zipfile.ZipFile(zbuf, "w") as zf:
        zf.writestr("f", b"x")
    assert _sniff_format(zbuf.getvalue(), None) == "zip"

    tbuf = io.BytesIO()
    with tarfile.open(fileobj=tbuf, mode="w") as tf:
        info = tarfile.TarInfo("f")
        info.size = 1
        tf.addfile(info, io.BytesIO(b"x"))
    assert _sniff_format(tbuf.getvalue(), None) == "tar"

    gbuf = io.BytesIO()
    with tarfile.open(fileobj=gbuf, mode="w:gz") as tf:
        info = tarfile.TarInfo("f")
        info.size = 1
        tf.addfile(info, io.BytesIO(b"x"))
    assert _sniff_format(gbuf.getvalue(), None) == "tar.gz"

    # Unknown bytes fall back to URI extension, else raise.
    assert _sniff_format(b"garbage", "aakaar://t/x/a.zip") == "zip"
    assert _sniff_format(b"garbage", "aakaar://t/x/a.tgz") == "tar.gz"
    with pytest.raises(RuntimeError, match="could not determine"):
        _sniff_format(b"garbage", None)
