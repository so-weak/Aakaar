"""Tests for cap.sftp_transfer.

Two flavors are tested independently:
  - mode='rename': prefers posix_rename when overwrite=true and the
    extension is available; plain rename otherwise. Cross-FS rename
    failures surface a helpful 'retry with mode=copy' message.
  - mode='copy': streams bytes through the worker, source left in place.

Shared invariants:
  - trailing-slash destination appends the source basename
  - overwrite=false aborts when the target exists
  - make_parents calls makedirs for the destination parent
  - invalid mode rejected at the handler boundary
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aakar.capabilities.sftp_transfer import handler
from tests._sftp_fakes import (
    FakeSftpAttrs,
    FakeSftpClient,
    make_activity_context,
    make_holder,
)


@pytest.mark.asyncio
async def test_rename_happy_path(tmp_path: Path) -> None:
    ctx = make_activity_context(tmp_path)
    sftp = FakeSftpClient(files={"/a/file.bin": b"x"})
    sid, _ = make_holder(ctx, sftp=sftp)

    out = await handler(
        ctx,
        {"session": sid, "src_path": "/a/file.bin", "dst_path": "/b/file.bin"},
    )
    assert out == {
        "src_path": "/a/file.bin",
        "dst_path": "/b/file.bin",
        "mode": "rename",
    }
    assert sftp.rename_calls == [("/a/file.bin", "/b/file.bin")]
    assert sftp.posix_rename_calls == []


@pytest.mark.asyncio
async def test_rename_overwrite_prefers_posix_rename(tmp_path: Path) -> None:
    """SFTP's plain `rename` is allowed to refuse existing targets;
    posix_rename (an SFTPv4+ extension) explicitly clobbers. We pick
    the latter when overwrite=true and the extension is exposed."""
    ctx = make_activity_context(tmp_path)
    sftp = FakeSftpClient(
        files={"/src": b"new"},
        stats={"/dst": FakeSftpAttrs(type=1, size=1)},
    )
    sid, _ = make_holder(ctx, sftp=sftp)
    await handler(
        ctx,
        {
            "session": sid,
            "src_path": "/src",
            "dst_path": "/dst",
            "overwrite": True,
        },
    )
    assert sftp.posix_rename_calls == [("/src", "/dst")]
    assert sftp.rename_calls == []


@pytest.mark.asyncio
async def test_rename_overwrite_falls_back_when_posix_rename_unavailable(
    tmp_path: Path,
) -> None:
    """Older asyncssh / older SFTP servers don't expose posix_rename;
    the capability should fall back to plain rename rather than
    AttributeError-ing."""
    ctx = make_activity_context(tmp_path)
    sftp = FakeSftpClient(
        files={"/src": b"new"},
        stats={"/dst": FakeSftpAttrs(type=1, size=1)},
        posix_rename_available=False,
    )
    sid, _ = make_holder(ctx, sftp=sftp)
    await handler(
        ctx,
        {
            "session": sid,
            "src_path": "/src",
            "dst_path": "/dst",
            "overwrite": True,
        },
    )
    assert sftp.rename_calls == [("/src", "/dst")]


@pytest.mark.asyncio
async def test_rename_failure_surfaces_copy_hint(tmp_path: Path) -> None:
    ctx = make_activity_context(tmp_path)
    sftp = FakeSftpClient(
        files={"/src": b"x"},
        rename_error=RuntimeError("EXDEV"),
    )
    sid, _ = make_holder(ctx, sftp=sftp, host="srv.test")
    with pytest.raises(RuntimeError, match="retry with mode='copy'"):
        await handler(
            ctx,
            {"session": sid, "src_path": "/src", "dst_path": "/dst"},
        )


@pytest.mark.asyncio
async def test_copy_streams_bytes_and_leaves_source(tmp_path: Path) -> None:
    ctx = make_activity_context(tmp_path)
    sftp = FakeSftpClient(files={"/src": b"payload-bytes"})
    sid, _ = make_holder(ctx, sftp=sftp)

    out = await handler(
        ctx,
        {
            "session": sid,
            "src_path": "/src",
            "dst_path": "/dst",
            "mode": "copy",
        },
    )
    assert out["mode"] == "copy"
    assert sftp.files["/src"] == b"payload-bytes"  # untouched
    assert sftp.files["/dst"] == b"payload-bytes"
    # No rename calls — copy is the path that ran.
    assert sftp.rename_calls == []
    assert sftp.posix_rename_calls == []


@pytest.mark.asyncio
async def test_dst_trailing_slash_appends_src_basename(tmp_path: Path) -> None:
    ctx = make_activity_context(tmp_path)
    sftp = FakeSftpClient(files={"/a/b/file.csv": b"x"})
    sid, _ = make_holder(ctx, sftp=sftp)
    out = await handler(
        ctx,
        {"session": sid, "src_path": "/a/b/file.csv", "dst_path": "/dest/"},
    )
    assert out["dst_path"] == "/dest/file.csv"
    assert sftp.rename_calls == [("/a/b/file.csv", "/dest/file.csv")]


@pytest.mark.asyncio
async def test_overwrite_false_refuses_existing_target(tmp_path: Path) -> None:
    ctx = make_activity_context(tmp_path)
    sftp = FakeSftpClient(
        files={"/src": b"new"},
        stats={"/dst": FakeSftpAttrs(type=1, size=1)},
    )
    sid, _ = make_holder(ctx, sftp=sftp, host="srv.test")
    with pytest.raises(RuntimeError, match="already exists on srv.test"):
        await handler(
            ctx,
            {"session": sid, "src_path": "/src", "dst_path": "/dst"},
        )
    # Nothing was attempted.
    assert sftp.rename_calls == []
    assert sftp.posix_rename_calls == []


@pytest.mark.asyncio
async def test_make_parents_for_dst(tmp_path: Path) -> None:
    ctx = make_activity_context(tmp_path)
    sftp = FakeSftpClient(files={"/src": b"x"})
    sid, _ = make_holder(ctx, sftp=sftp)
    await handler(
        ctx,
        {
            "session": sid,
            "src_path": "/src",
            "dst_path": "/deep/path/here/file.bin",
            "make_parents": True,
        },
    )
    assert sftp.makedirs_calls == [("/deep/path/here", True)]


@pytest.mark.asyncio
async def test_invalid_mode_rejected(tmp_path: Path) -> None:
    ctx = make_activity_context(tmp_path)
    sid, _ = make_holder(ctx)
    with pytest.raises(ValueError, match="mode must be"):
        await handler(
            ctx,
            {
                "session": sid,
                "src_path": "/a",
                "dst_path": "/b",
                "mode": "yeet",
            },
        )


@pytest.mark.asyncio
async def test_empty_paths_rejected(tmp_path: Path) -> None:
    ctx = make_activity_context(tmp_path)
    sid, _ = make_holder(ctx)
    with pytest.raises(ValueError, match="non-empty"):
        await handler(
            ctx, {"session": sid, "src_path": "", "dst_path": "/b"}
        )
    with pytest.raises(ValueError, match="non-empty"):
        await handler(
            ctx, {"session": sid, "src_path": "/a", "dst_path": "  "}
        )


@pytest.mark.asyncio
async def test_missing_session_raises(tmp_path: Path) -> None:
    ctx = make_activity_context(tmp_path)
    with pytest.raises(RuntimeError, match="no live sftp session"):
        await handler(
            ctx, {"session": "missing", "src_path": "/a", "dst_path": "/b"}
        )
