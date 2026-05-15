"""Tests for cap.sftp_write.

Covers:
  - file_uri must be aakar:// (refuse arbitrary local paths)
  - trailing-slash destination appends the recovered user-facing basename
  - overwrite=false aborts on stat()-found target; overwrite=true clobbers
  - make_parents calls sftp.makedirs once for the parent dir
  - bytes hit the server intact via streaming writes
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aakar.capabilities.sftp_write import _user_facing_basename, handler
from tests._sftp_fakes import (
    FakeSftpAttrs,
    FakeSftpClient,
    make_activity_context,
    make_holder,
)


@pytest.mark.asyncio
async def test_happy_path_writes_bytes(tmp_path: Path) -> None:
    ctx = make_activity_context(tmp_path)
    payload = b"row1\nrow2\n"
    obj = ctx.object_store.put(str(ctx.tenant_id), "stage/up.csv", payload)

    sftp = FakeSftpClient()
    sid, _ = make_holder(ctx, sftp=sftp, host="srv.test")

    out = await handler(
        ctx,
        {
            "session": sid,
            "file_uri": obj.uri,
            "remote_path": "/incoming/up.csv",
        },
    )
    assert out == {"remote_path": "/incoming/up.csv", "size": len(payload)}
    # The file lives on the fake server with the right bytes.
    assert sftp.files["/incoming/up.csv"] == payload
    # No directory creation requested (make_parents is off by default).
    assert sftp.makedirs_calls == []


@pytest.mark.asyncio
async def test_rejects_non_managed_uri(tmp_path: Path) -> None:
    ctx = make_activity_context(tmp_path)
    sid, _ = make_holder(ctx)
    with pytest.raises(ValueError, match="aakar://"):
        await handler(
            ctx,
            {
                "session": sid,
                "file_uri": "file:///etc/passwd",
                "remote_path": "/x",
            },
        )


@pytest.mark.asyncio
async def test_trailing_slash_appends_basename(tmp_path: Path) -> None:
    """Storage key follows the `<uuid32hex>_<original>` shape used by
    file.read_local / cap.file_download — when the caller passes a
    directory as remote_path, the original filename gets glued on the
    end, not the uuid-prefixed key."""
    ctx = make_activity_context(tmp_path)
    # Force a `<hex32>_<name>` key by writing it through the object
    # store under a path that already has the right shape.
    key = "stage/" + "a" * 32 + "_report.csv"
    obj = ctx.object_store.put(str(ctx.tenant_id), key, b"data")

    sftp = FakeSftpClient()
    sid, _ = make_holder(ctx, sftp=sftp)

    out = await handler(
        ctx,
        {"session": sid, "file_uri": obj.uri, "remote_path": "/in/"},
    )
    assert out["remote_path"] == "/in/report.csv"
    assert sftp.files["/in/report.csv"] == b"data"


@pytest.mark.asyncio
async def test_overwrite_false_refuses_existing_target(tmp_path: Path) -> None:
    ctx = make_activity_context(tmp_path)
    obj = ctx.object_store.put(str(ctx.tenant_id), "stage/x", b"new")

    sftp = FakeSftpClient(
        stats={"/out/x": FakeSftpAttrs(type=1, size=4)},
        files={"/out/x": b"orig"},
    )
    sid, _ = make_holder(ctx, sftp=sftp, host="srv.test")
    with pytest.raises(RuntimeError, match="already exists on srv.test"):
        await handler(
            ctx,
            {"session": sid, "file_uri": obj.uri, "remote_path": "/out/x"},
        )
    # Existing file is untouched.
    assert sftp.files["/out/x"] == b"orig"


@pytest.mark.asyncio
async def test_overwrite_true_clobbers(tmp_path: Path) -> None:
    ctx = make_activity_context(tmp_path)
    obj = ctx.object_store.put(str(ctx.tenant_id), "stage/x", b"new")

    sftp = FakeSftpClient(
        stats={"/out/x": FakeSftpAttrs(type=1, size=4)},
        files={"/out/x": b"orig"},
    )
    sid, _ = make_holder(ctx, sftp=sftp)
    await handler(
        ctx,
        {
            "session": sid,
            "file_uri": obj.uri,
            "remote_path": "/out/x",
            "overwrite": True,
        },
    )
    assert sftp.files["/out/x"] == b"new"


@pytest.mark.asyncio
async def test_make_parents_calls_makedirs_for_parent_only(tmp_path: Path) -> None:
    ctx = make_activity_context(tmp_path)
    obj = ctx.object_store.put(str(ctx.tenant_id), "stage/x", b"d")

    sftp = FakeSftpClient()
    sid, _ = make_holder(ctx, sftp=sftp)

    await handler(
        ctx,
        {
            "session": sid,
            "file_uri": obj.uri,
            "remote_path": "/a/b/c/file.bin",
            "make_parents": True,
        },
    )
    assert sftp.makedirs_calls == [("/a/b/c", True)]


@pytest.mark.asyncio
async def test_make_parents_skipped_when_remote_at_root(tmp_path: Path) -> None:
    """Writing to '/file.bin' shouldn't trigger makedirs('/') — the root
    always exists and asyncssh chokes on it on some servers."""
    ctx = make_activity_context(tmp_path)
    obj = ctx.object_store.put(str(ctx.tenant_id), "stage/x", b"d")
    sftp = FakeSftpClient()
    sid, _ = make_holder(ctx, sftp=sftp)
    await handler(
        ctx,
        {
            "session": sid,
            "file_uri": obj.uri,
            "remote_path": "/file.bin",
            "make_parents": True,
        },
    )
    assert sftp.makedirs_calls == []


@pytest.mark.asyncio
async def test_make_parents_propagates_makedirs_failure(tmp_path: Path) -> None:
    ctx = make_activity_context(tmp_path)
    obj = ctx.object_store.put(str(ctx.tenant_id), "stage/x", b"d")

    class _Sftp(FakeSftpClient):
        async def makedirs(self, path: str, exist_ok: bool = False) -> None:
            raise PermissionError("no mkdir for you")

    sftp = _Sftp()
    sid, _ = make_holder(ctx, sftp=sftp, host="srv.test")
    with pytest.raises(RuntimeError, match="could not create parent dir '/a/b' on srv.test"):
        await handler(
            ctx,
            {
                "session": sid,
                "file_uri": obj.uri,
                "remote_path": "/a/b/c",
                "make_parents": True,
            },
        )


@pytest.mark.asyncio
async def test_empty_remote_path_rejected(tmp_path: Path) -> None:
    ctx = make_activity_context(tmp_path)
    obj = ctx.object_store.put(str(ctx.tenant_id), "stage/x", b"d")
    sid, _ = make_holder(ctx)
    with pytest.raises(ValueError, match="non-empty"):
        await handler(
            ctx,
            {"session": sid, "file_uri": obj.uri, "remote_path": ""},
        )


def test_user_facing_basename_handles_uuid_prefix_and_fallback() -> None:
    uuid_prefixed = (
        "aakar://t/abc/runs/r/sftp/" + "f" * 32 + "_invoices_2026_05.xml"
    )
    assert _user_facing_basename(uuid_prefixed) == "invoices_2026_05.xml"
    assert _user_facing_basename("aakar://t/abc/stage/plain.csv") == "plain.csv"
    assert _user_facing_basename("") == "upload.bin"
