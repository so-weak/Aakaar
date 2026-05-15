"""Tests for cap.sftp_read.

Covers:
  - happy path stores bytes in managed storage under a per-run key
  - reported-size pre-check rejects oversize files without streaming
  - mid-stream cap trips when the server doesn't report size
  - stat() failure surfaces a runtime error against the remote path
  - filename in the output is the remote basename
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aakar.capabilities.sftp_read import handler
from aakar.storage.object_store import parse_uri
from tests._sftp_fakes import (
    FakeSftpAttrs,
    FakeSftpClient,
    make_activity_context,
    make_holder,
)


@pytest.mark.asyncio
async def test_read_stores_under_runs_key_and_returns_uri(tmp_path: Path) -> None:
    ctx = make_activity_context(tmp_path)
    payload = b"col_a,col_b\n1,2\n3,4\n"
    sftp = FakeSftpClient(
        files={"/exports/report.csv": payload},
        stats={"/exports/report.csv": FakeSftpAttrs(type=1, size=len(payload))},
    )
    sid, _ = make_holder(ctx, sftp=sftp)

    out = await handler(
        ctx, {"session": sid, "remote_path": "/exports/report.csv"}
    )
    assert out["filename"] == "report.csv"
    assert out["size"] == len(payload)

    # The URI round-trips through the object store and the bytes match.
    assert out["uri"].startswith("aakar://")
    tenant, key = parse_uri(out["uri"])
    assert tenant == str(ctx.tenant_id)
    assert key.startswith(f"runs/{ctx.run_id}/sftp/")
    assert key.endswith("_report.csv")
    assert ctx.object_store.get(out["uri"]) == payload


@pytest.mark.asyncio
async def test_reported_size_over_cap_short_circuits(tmp_path: Path) -> None:
    """When `stat()` reports a size > max_bytes we bail before opening
    the file — there's no point streaming bytes we'll reject."""
    ctx = make_activity_context(tmp_path)
    sftp = FakeSftpClient(
        files={"/huge.bin": b"x" * 10},  # won't be touched
        stats={"/huge.bin": FakeSftpAttrs(type=1, size=999_999_999)},
    )
    sid, _ = make_holder(ctx, sftp=sftp)
    with pytest.raises(RuntimeError, match="max_bytes=1024"):
        await handler(
            ctx,
            {"session": sid, "remote_path": "/huge.bin", "max_bytes": 1024},
        )
    # open() never ran — we short-circuited after stat.
    assert all(c[0] != "open" for c in sftp.calls)


@pytest.mark.asyncio
async def test_mid_stream_cap_when_size_not_reported(tmp_path: Path) -> None:
    """Servers that don't return size still get bounded — the streaming
    loop's running tally trips when the total exceeds max_bytes."""
    ctx = make_activity_context(tmp_path)
    payload = b"y" * (5 * 1024)
    sftp = FakeSftpClient(
        files={"/u.bin": payload},
        stats={"/u.bin": FakeSftpAttrs(type=1, size=None)},
    )
    sid, _ = make_holder(ctx, sftp=sftp)
    with pytest.raises(RuntimeError, match="exceeded max_bytes=1024 mid-stream"):
        await handler(
            ctx,
            {"session": sid, "remote_path": "/u.bin", "max_bytes": 1024},
        )


@pytest.mark.asyncio
async def test_stat_failure_surfaces_with_remote_path(tmp_path: Path) -> None:
    ctx = make_activity_context(tmp_path)
    sftp = FakeSftpClient(
        stats={"/nope.csv": PermissionError("denied")}
    )
    sid, _ = make_holder(ctx, sftp=sftp, host="srv.test")
    with pytest.raises(RuntimeError, match=r"cannot stat '/nope.csv' on srv.test"):
        await handler(ctx, {"session": sid, "remote_path": "/nope.csv"})


@pytest.mark.asyncio
async def test_empty_remote_path_rejected(tmp_path: Path) -> None:
    ctx = make_activity_context(tmp_path)
    sid, _ = make_holder(ctx)
    with pytest.raises(ValueError, match="non-empty"):
        await handler(ctx, {"session": sid, "remote_path": ""})


@pytest.mark.asyncio
async def test_filename_defaults_when_path_has_no_basename(tmp_path: Path) -> None:
    """`PurePosixPath('/').name` is '' — the only realistic path that
    triggers the fallback once normalize_remote_path has rejected empty
    inputs. Doesn't happen in practice (you don't `read` a directory)
    but the defensive fallback should still produce a valid filename."""
    ctx = make_activity_context(tmp_path)
    sftp = FakeSftpClient(
        files={"/": b"hi"},
        stats={"/": FakeSftpAttrs(type=1, size=2)},
    )
    sid, _ = make_holder(ctx, sftp=sftp)
    out = await handler(ctx, {"session": sid, "remote_path": "/"})
    assert out["filename"] == "download.bin"


@pytest.mark.asyncio
async def test_missing_session_raises(tmp_path: Path) -> None:
    ctx = make_activity_context(tmp_path)
    with pytest.raises(RuntimeError, match="no live sftp session"):
        await handler(ctx, {"session": "missing", "remote_path": "/x"})
