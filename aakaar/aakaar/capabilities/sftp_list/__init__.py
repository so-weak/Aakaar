"""cap.sftp_list — list a remote directory over an SFTP session.

Walks one directory (optionally recursive) and returns an array of
entries with kind/size/mtime. The capability does NOT log in; it
expects a `session` produced by an upstream `cap.sftp_login`.

Filtering:
  - `pattern`: optional glob (`*.csv`, `2026-05-*.xml`) applied to the
    basename. Glob is matched in-process with `fnmatch` so the
    semantics match Python and are consistent across SFTP servers
    (OpenSSH SFTP doesn't standardize server-side glob).
  - `kinds`: which entry kinds to keep — any subset of
    `('file', 'dir', 'symlink')`. Defaults to all three.

Recursion:
  - `recursive=true` descends subdirectories breadth-first. Each
    entry's `path` is the *absolute* remote path so downstream nodes
    can feed it back into cap.sftp_read / cap.sftp_transfer without
    needing to track a base.
  - `max_entries` caps the result (default 5000) so a misaimed
    recursion against `/` can't blow up the run; exceeding the cap is
    a node failure with a clear message rather than a silent truncate.

Hidden entries (names starting with `.`) are included by default —
that's where most config / dotfiles live. Set `include_hidden=false`
to filter them out.
"""

from __future__ import annotations

import fnmatch
import logging
from collections import deque
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from aakaar.capabilities._sftp_session import get_holder, normalize_remote_path
from aakaar.interpreter.activities.types import ActivityContext
from aakaar.shared.registry import CapabilityDefinition

logger = logging.getLogger(__name__)
CAP_REF = "cap.sftp_list"

_DEFAULT_MAX_ENTRIES = 5000
_VALID_KINDS = ("file", "dir", "symlink")


class _Inputs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session: str = Field(
        description="SFTP session handle from cap.sftp_login, e.g. ${login.session}."
    )
    path: str = Field(
        description="Remote directory to list. Absolute paths recommended."
    )
    pattern: str | None = Field(
        default=None,
        description=(
            "Optional glob pattern matched against entry basenames (e.g. "
            "'*.csv', 'report-*.xml'). Matched in-process with fnmatch."
        ),
    )
    recursive: bool = Field(
        default=False,
        description="Descend into subdirectories breadth-first.",
    )
    kinds: list[str] = Field(
        default_factory=lambda: list(_VALID_KINDS),
        description=(
            "Which entry kinds to keep. Subset of 'file', 'dir', 'symlink'. "
            "Defaults to all three."
        ),
    )
    include_hidden: bool = Field(
        default=True,
        description="Include entries whose basename starts with '.'.",
    )
    max_entries: int = Field(
        default=_DEFAULT_MAX_ENTRIES,
        ge=1,
        le=100000,
        description=(
            "Hard cap on the number of entries returned. Exceeding the cap "
            "fails the node rather than silently truncating."
        ),
    )


class _Entry(BaseModel):
    path: str = Field(description="Absolute remote path.")
    name: str = Field(description="Basename of the entry.")
    kind: str = Field(description="One of 'file', 'dir', 'symlink', 'other'.")
    size: int | None = Field(
        default=None, description="Size in bytes for regular files; null otherwise."
    )
    mtime: float | None = Field(
        default=None, description="POSIX mtime in seconds since epoch."
    )


class _Outputs(BaseModel):
    entries: list[_Entry] = Field(description="Matched entries, sorted by path.")
    count: int = Field(description="Length of `entries`.")
    truncated: bool = Field(
        description=(
            "Always false in the current implementation — we raise instead of "
            "truncating when max_entries is hit. Present so the planner can "
            "branch on this field once richer modes are added."
        )
    )


definition = CapabilityDefinition(
    ref=CAP_REF,
    description=(
        "List a remote directory over an authenticated SFTP session, "
        "optionally recursive and filtered by glob. Returns entries with "
        "path/kind/size/mtime."
    ),
    input_schema=_Inputs,
    output_schema=_Outputs,
    secrets=(),
    tags=("sftp", "traversal"),
)


async def handler(ctx: ActivityContext, inputs: dict[str, Any]) -> dict[str, Any]:
    holder = get_holder(ctx, inputs["session"])
    base = normalize_remote_path(inputs["path"])
    pattern = inputs.get("pattern")
    recursive = bool(inputs.get("recursive", False))
    kinds = inputs.get("kinds") or list(_VALID_KINDS)
    include_hidden = bool(inputs.get("include_hidden", True))
    max_entries = int(inputs.get("max_entries", _DEFAULT_MAX_ENTRIES))

    bad = [k for k in kinds if k not in _VALID_KINDS]
    if bad:
        raise ValueError(
            f"cap.sftp_list: unknown kind(s) {bad!r}; allowed: {list(_VALID_KINDS)}"
        )
    keep_kinds = set(kinds)

    logger.info(
        "cap.sftp_list start session=%s path=%s pattern=%r recursive=%s",
        inputs["session"],
        base,
        pattern,
        recursive,
    )

    out: list[dict[str, Any]] = []
    queue: deque[str] = deque([base])
    visited_dirs: set[str] = set()

    while queue:
        cur = queue.popleft()
        if cur in visited_dirs:
            continue
        visited_dirs.add(cur)

        try:
            raw = await holder.sftp.readdir(cur)
        except Exception as e:
            # If the *root* listing fails, that's a user-facing error —
            # the path is wrong or unreadable. If a *descendant* fails
            # mid-walk, log and continue: permission boundaries inside
            # a tree are normal and shouldn't kill the whole listing.
            if cur == base:
                raise RuntimeError(
                    f"cap.sftp_list: could not read {cur!r} on {holder.host}: {e}"
                ) from e
            logger.info(
                "cap.sftp_list skip unreadable dir=%s err=%s", cur, type(e).__name__
            )
            continue

        for entry in raw:
            name = getattr(entry, "filename", None)
            if not name or name in (".", ".."):
                continue
            if not include_hidden and name.startswith("."):
                continue
            child = _join_remote(cur, name)

            attrs = getattr(entry, "attrs", None)
            kind = _entry_kind(attrs)
            size = getattr(attrs, "size", None) if attrs else None
            mtime = getattr(attrs, "mtime", None) if attrs else None

            if recursive and kind == "dir":
                queue.append(child)

            if kind not in keep_kinds:
                continue
            if pattern and not fnmatch.fnmatch(name, pattern):
                continue

            out.append(
                {
                    "path": child,
                    "name": name,
                    "kind": kind,
                    "size": int(size) if isinstance(size, int) else None,
                    "mtime": float(mtime) if isinstance(mtime, int | float) else None,
                }
            )

            if len(out) > max_entries:
                raise RuntimeError(
                    f"cap.sftp_list: more than max_entries={max_entries} entries "
                    f"matched under {base!r}; narrow the path/pattern or raise the cap"
                )

    out.sort(key=lambda e: e["path"])
    logger.info(
        "cap.sftp_list ok session=%s path=%s count=%d", inputs["session"], base, len(out)
    )
    return {"entries": out, "count": len(out), "truncated": False}


def _entry_kind(attrs: Any) -> str:
    """Map asyncssh SFTPAttrs.permissions to a kind label.

    asyncssh exposes a `type` field on SFTPAttrs in newer versions
    (FILETYPE_REGULAR / FILETYPE_DIRECTORY / FILETYPE_SYMLINK), and a
    POSIX `permissions` field in all versions. We check `type` first
    when present and fall back to the permission bits.
    """
    if attrs is None:
        return "other"
    t = getattr(attrs, "type", None)
    if t is not None:
        # Constants from asyncssh.sftp.FILEXFER_TYPE_*
        # 1 regular, 2 directory, 3 symlink, others = special
        if t == 1:
            return "file"
        if t == 2:
            return "dir"
        if t == 3:
            return "symlink"
        return "other"
    perms = getattr(attrs, "permissions", None)
    if perms is None:
        return "other"
    import stat as _stat

    if _stat.S_ISDIR(perms):
        return "dir"
    if _stat.S_ISLNK(perms):
        return "symlink"
    if _stat.S_ISREG(perms):
        return "file"
    return "other"


def _join_remote(parent: str, name: str) -> str:
    """POSIX-style join — SFTP paths are slash-separated regardless of
    the worker host's OS. Avoids os.path.join's Windows quirks."""
    if parent.endswith("/"):
        return parent + name
    return parent + "/" + name
