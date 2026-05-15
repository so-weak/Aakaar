"""Tests for cap.sftp_list.

Exercises directory traversal with the fake SFTP client:
  - flat listing with hidden/pattern/kind filters
  - recursive BFS that produces absolute paths
  - max_entries cap (fails the node)
  - unreadable root vs. unreadable descendant (raise vs. continue)
  - schema-level kind validation
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aakar.capabilities.sftp_list import handler
from tests._sftp_fakes import (
    FakeSftpAttrs,
    FakeSftpClient,
    FakeSftpEntry,
    make_activity_context,
    make_holder,
)


def _file(name: str, size: int = 100) -> FakeSftpEntry:
    return FakeSftpEntry(filename=name, attrs=FakeSftpAttrs(type=1, size=size, mtime=1_700_000_000.0))


def _dir(name: str) -> FakeSftpEntry:
    return FakeSftpEntry(filename=name, attrs=FakeSftpAttrs(type=2))


def _link(name: str) -> FakeSftpEntry:
    return FakeSftpEntry(filename=name, attrs=FakeSftpAttrs(type=3))


@pytest.mark.asyncio
async def test_flat_listing_returns_sorted_entries_with_attrs(tmp_path: Path) -> None:
    ctx = make_activity_context(tmp_path)
    sftp = FakeSftpClient(
        listings={
            "/inbox": [
                _file("b.csv", size=10),
                _file("a.csv", size=20),
                FakeSftpEntry(filename=".", attrs=FakeSftpAttrs(type=2)),
                FakeSftpEntry(filename="..", attrs=FakeSftpAttrs(type=2)),
            ]
        }
    )
    sid, _ = make_holder(ctx, sftp=sftp)

    out = await handler(ctx, {"session": sid, "path": "/inbox"})
    paths = [e["path"] for e in out["entries"]]
    assert paths == ["/inbox/a.csv", "/inbox/b.csv"]  # sorted, '.'/'..' dropped
    assert out["count"] == 2
    assert out["truncated"] is False
    a = out["entries"][0]
    assert a["kind"] == "file"
    assert a["size"] == 20
    assert a["mtime"] == 1_700_000_000.0


@pytest.mark.asyncio
async def test_pattern_filter_uses_fnmatch(tmp_path: Path) -> None:
    ctx = make_activity_context(tmp_path)
    sftp = FakeSftpClient(
        listings={
            "/r": [_file("a.csv"), _file("b.txt"), _file("c.csv"), _dir("sub")]
        }
    )
    sid, _ = make_holder(ctx, sftp=sftp)
    out = await handler(ctx, {"session": sid, "path": "/r", "pattern": "*.csv"})
    assert [e["name"] for e in out["entries"]] == ["a.csv", "c.csv"]


@pytest.mark.asyncio
async def test_kinds_filter_keeps_only_requested(tmp_path: Path) -> None:
    ctx = make_activity_context(tmp_path)
    sftp = FakeSftpClient(
        listings={"/r": [_file("a"), _dir("d"), _link("l")]}
    )
    sid, _ = make_holder(ctx, sftp=sftp)
    out = await handler(ctx, {"session": sid, "path": "/r", "kinds": ["dir"]})
    assert [(e["name"], e["kind"]) for e in out["entries"]] == [("d", "dir")]


@pytest.mark.asyncio
async def test_hidden_filter_drops_dotfiles_when_disabled(tmp_path: Path) -> None:
    ctx = make_activity_context(tmp_path)
    sftp = FakeSftpClient(
        listings={"/r": [_file(".secret"), _file("visible")]}
    )
    sid, _ = make_holder(ctx, sftp=sftp)
    out = await handler(
        ctx, {"session": sid, "path": "/r", "include_hidden": False}
    )
    assert [e["name"] for e in out["entries"]] == ["visible"]

    out2 = await handler(ctx, {"session": sid, "path": "/r"})  # default include
    assert {e["name"] for e in out2["entries"]} == {".secret", "visible"}


@pytest.mark.asyncio
async def test_recursive_bfs_produces_absolute_paths(tmp_path: Path) -> None:
    ctx = make_activity_context(tmp_path)
    sftp = FakeSftpClient(
        listings={
            "/r": [_dir("a"), _file("top.csv")],
            "/r/a": [_dir("b"), _file("mid.csv")],
            "/r/a/b": [_file("deep.csv")],
        }
    )
    sid, _ = make_holder(ctx, sftp=sftp)
    out = await handler(
        ctx, {"session": sid, "path": "/r", "recursive": True, "kinds": ["file"]}
    )
    assert [e["path"] for e in out["entries"]] == [
        "/r/a/b/deep.csv",
        "/r/a/mid.csv",
        "/r/top.csv",
    ]


@pytest.mark.asyncio
async def test_recursive_handles_trailing_slash_root(tmp_path: Path) -> None:
    """A path like '/r/' should join children as '/r/x', not '/r//x'."""
    ctx = make_activity_context(tmp_path)
    sftp = FakeSftpClient(listings={"/r/": [_file("x")]})
    sid, _ = make_holder(ctx, sftp=sftp)
    out = await handler(ctx, {"session": sid, "path": "/r/"})
    assert out["entries"][0]["path"] == "/r/x"


@pytest.mark.asyncio
async def test_unreadable_root_raises(tmp_path: Path) -> None:
    ctx = make_activity_context(tmp_path)
    sftp = FakeSftpClient(
        readdir_errors={"/forbidden": PermissionError("nope")}
    )
    sid, _ = make_holder(ctx, sftp=sftp)
    with pytest.raises(RuntimeError, match="could not read"):
        await handler(ctx, {"session": sid, "path": "/forbidden"})


@pytest.mark.asyncio
async def test_unreadable_descendant_is_skipped(tmp_path: Path) -> None:
    """Permission denied on a subdir mid-walk is normal: drop that
    branch and keep going. The error is logged but doesn't fail the
    node."""
    ctx = make_activity_context(tmp_path)
    sftp = FakeSftpClient(
        listings={
            "/r": [_dir("ok"), _dir("locked"), _file("top")],
            "/r/ok": [_file("inside")],
        },
        readdir_errors={"/r/locked": PermissionError("denied")},
    )
    sid, _ = make_holder(ctx, sftp=sftp)
    out = await handler(
        ctx, {"session": sid, "path": "/r", "recursive": True, "kinds": ["file"]}
    )
    assert sorted(e["path"] for e in out["entries"]) == ["/r/ok/inside", "/r/top"]


@pytest.mark.asyncio
async def test_max_entries_cap_fails_the_node(tmp_path: Path) -> None:
    ctx = make_activity_context(tmp_path)
    sftp = FakeSftpClient(
        listings={"/r": [_file(f"f{i}") for i in range(10)]}
    )
    sid, _ = make_holder(ctx, sftp=sftp)
    with pytest.raises(RuntimeError, match="max_entries=3"):
        await handler(ctx, {"session": sid, "path": "/r", "max_entries": 3})


@pytest.mark.asyncio
async def test_unknown_kind_is_rejected(tmp_path: Path) -> None:
    ctx = make_activity_context(tmp_path)
    sftp = FakeSftpClient(listings={"/r": [_file("x")]})
    sid, _ = make_holder(ctx, sftp=sftp)
    with pytest.raises(ValueError, match="unknown kind"):
        await handler(ctx, {"session": sid, "path": "/r", "kinds": ["socket"]})


@pytest.mark.asyncio
async def test_missing_session_handle_raises(tmp_path: Path) -> None:
    ctx = make_activity_context(tmp_path)
    with pytest.raises(RuntimeError, match="no live sftp session"):
        await handler(ctx, {"session": "bogus", "path": "/r"})


@pytest.mark.asyncio
async def test_empty_path_rejected_by_normalize(tmp_path: Path) -> None:
    ctx = make_activity_context(tmp_path)
    sid, _ = make_holder(ctx)
    with pytest.raises(ValueError, match="non-empty"):
        await handler(ctx, {"session": sid, "path": "   "})


@pytest.mark.asyncio
async def test_kind_detected_from_permission_bits_when_type_missing(
    tmp_path: Path,
) -> None:
    """asyncssh occasionally returns SFTPAttrs without `type` (older
    protocol versions); we fall back to stat-style permission bits."""
    import stat as _stat

    ctx = make_activity_context(tmp_path)
    sftp = FakeSftpClient(
        listings={
            "/r": [
                FakeSftpEntry(
                    filename="dir",
                    attrs=FakeSftpAttrs(type=None, permissions=_stat.S_IFDIR | 0o755),
                ),
                FakeSftpEntry(
                    filename="reg",
                    attrs=FakeSftpAttrs(type=None, permissions=_stat.S_IFREG | 0o644),
                ),
            ]
        }
    )
    sid, _ = make_holder(ctx, sftp=sftp)
    out = await handler(ctx, {"session": sid, "path": "/r"})
    kinds = {e["name"]: e["kind"] for e in out["entries"]}
    assert kinds == {"dir": "dir", "reg": "file"}
