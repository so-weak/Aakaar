"""cap.archive_manage — create / extract / list zip and tar archives.

A self-contained, server-local file utility built on the Python standard
library (`zipfile`, `tarfile`) so it has no third-party dependencies. The
modules are imported lazily inside the handler purely for symmetry with the
other file capabilities; stdlib imports never fail, so there's no optional
dependency to guard.

Three operations, selected by `op`:

  create   Bundle one or more `aakaar://` source objects into a single
           archive and write it back to object storage. Each source's
           archive entry name is the last path segment of its storage key
           (e.g. `aakaar://t/<tenant>/runs/<id>/out/report.csv` becomes
           `report.csv`). Duplicate basenames are disambiguated with a
           numeric suffix so no entry silently overwrites another.
           Returns `{archive_uri}`.

  list     Read an existing archive and return its entry metadata without
           extracting anything. Returns `{entries}`.

  extract  Read an existing archive, write every regular-file member back
           to object storage under a per-run prefix, and return the new
           `aakaar://` URIs. Directory members and anything that resolves
           outside the extraction root (path traversal, absolute paths,
           symlinks) are refused. Returns `{extracted_uris}` and `{entries}`.

`format` is `zip`, `tar` (uncompressed) or `tar.gz`. For `create` it is
required and picks the container/compression. For `list`/`extract` it is
optional: when omitted the format is sniffed from the archive bytes, with
the `archive` URI's extension as a tiebreaker.

Everything happens on the worker host via a `TemporaryDirectory`; no bytes
leave the machine and no remote dispatch occurs.
"""

from __future__ import annotations

import logging
import os
import posixpath
import tempfile
import uuid
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from aakaar.interpreter.activities.types import ActivityContext
from aakaar.shared.registry import CapabilityDefinition
from aakaar.storage.object_store import parse_uri

logger = logging.getLogger(__name__)
CAP_REF = "cap.archive_manage"

_FORMATS = ("zip", "tar", "tar.gz")
# tarfile write modes keyed by our `format` value.
_TAR_WRITE_MODE = {"tar": "w", "tar.gz": "w:gz"}


class _Inputs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    op: Literal["create", "extract", "list"] = Field(
        description="What to do: build an archive, unpack one, or enumerate entries."
    )
    sources: list[str] | None = Field(
        default=None,
        description=(
            "For op='create': the aakaar:// URIs to bundle, in order. "
            "Ignored (and must be omitted) for extract/list."
        ),
    )
    archive: str | None = Field(
        default=None,
        description=(
            "For op='extract'/'list': the aakaar:// URI of the archive to "
            "read. Ignored (and must be omitted) for create."
        ),
    )
    format: Literal["zip", "tar", "tar.gz"] | None = Field(
        default=None,
        description=(
            "Archive container/compression. Required for create. Optional for "
            "extract/list, where it is sniffed from the archive when omitted."
        ),
    )


class _Entry(BaseModel):
    name: str = Field(description="Archive member path (as stored in the archive).")
    size: int = Field(description="Uncompressed size in bytes.")
    is_dir: bool = Field(description="True for directory members.")


class _Outputs(BaseModel):
    op: str = Field(description="Echo of the requested operation.")
    format: str = Field(description="Resolved archive format.")
    archive_uri: str | None = Field(
        default=None, description="aakaar:// URI of the created archive (op='create')."
    )
    entries: list[_Entry] = Field(
        default_factory=list,
        description="Member metadata (op='list', and also returned for extract).",
    )
    extracted_uris: list[str] = Field(
        default_factory=list,
        description="aakaar:// URIs of the extracted regular files (op='extract').",
    )


definition = CapabilityDefinition(
    ref=CAP_REF,
    description=(
        "Create, extract, or list zip / tar / tar.gz archives entirely on the "
        "worker using the Python standard library. Reads and writes files "
        "through object storage; rejects unsafe (path-traversal/absolute/"
        "symlink) archive members on extract. No third-party dependencies."
    ),
    input_schema=_Inputs,
    output_schema=_Outputs,
    secrets=(),
    tags=("files", "archive", "zip", "tar"),
)


# ---------------------------------------------------------------------------
# Pure helpers (unit-testable without an ActivityContext)
# ---------------------------------------------------------------------------


def _basename_for_uri(uri: str) -> str:
    """The archive entry name for a source URI: the last segment of its key."""
    _tenant, key = parse_uri(uri)
    name = posixpath.basename(key.rstrip("/"))
    if not name:
        raise RuntimeError(f"cap.archive_manage: cannot derive a name from {uri!r}")
    return name


def _dedupe_names(names: list[str]) -> list[str]:
    """Make a list of basenames unique by appending ' (n)' before the suffix.

    Bundling two sources that both end in 'report.csv' must not clobber one
    another inside the archive, so collisions get a numeric tag.
    """
    seen: dict[str, int] = {}
    out: list[str] = []
    for name in names:
        if name not in seen:
            seen[name] = 0
            out.append(name)
            continue
        seen[name] += 1
        stem, dot, ext = name.partition(".")
        tagged = f"{stem} ({seen[name]}){dot}{ext}"
        # Guard against the (unlikely) case the tagged name also collides.
        while tagged in seen:
            seen[name] += 1
            tagged = f"{stem} ({seen[name]}){dot}{ext}"
        seen[tagged] = 0
        out.append(tagged)
    return out


def _sniff_format(data: bytes, archive_uri: str | None) -> str:
    """Detect the archive format from its leading bytes, falling back to the
    URI extension and finally erroring if nothing matches."""
    # zip: 'PK\x03\x04' (also empty/spanned variants PK\x05\x06 / PK\x07\x08).
    if data[:2] == b"PK":
        return "zip"
    # gzip: 0x1f 0x8b — for our purposes always a tar.gz.
    if data[:2] == b"\x1f\x8b":
        return "tar.gz"
    # ustar magic lives at offset 257 in a tar header block.
    if len(data) >= 265 and data[257:262] == b"ustar":
        return "tar"
    if archive_uri:
        lower = archive_uri.lower()
        if lower.endswith(".zip"):
            return "zip"
        if lower.endswith((".tar.gz", ".tgz")):
            return "tar.gz"
        if lower.endswith(".tar"):
            return "tar"
    raise RuntimeError(
        "cap.archive_manage: could not determine archive format; pass `format` "
        "explicitly (zip|tar|tar.gz)"
    )


def _is_unsafe_member(name: str) -> bool:
    """True if a member path would escape the extraction root."""
    if not name or name.startswith(("/", "\\")):
        return True
    # Windows drive prefix or UNC-ish absolute.
    if len(name) >= 2 and name[1] == ":":
        return True
    parts = name.replace("\\", "/").split("/")
    return any(p == ".." for p in parts)


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


async def handler(ctx: ActivityContext, inputs: dict[str, Any]) -> dict[str, Any]:
    import tarfile
    import zipfile

    op = inputs["op"]
    fmt = inputs.get("format")
    sources = inputs.get("sources")
    archive = inputs.get("archive")

    if op == "create":
        if not sources:
            raise RuntimeError("cap.archive_manage: op='create' requires `sources`")
        if archive:
            raise RuntimeError("cap.archive_manage: op='create' must not set `archive`")
        if not fmt:
            raise RuntimeError("cap.archive_manage: op='create' requires `format`")
    else:  # extract | list
        if not archive:
            raise RuntimeError(
                f"cap.archive_manage: op={op!r} requires `archive`"
            )
        if sources:
            raise RuntimeError(
                f"cap.archive_manage: op={op!r} must not set `sources`"
            )

    logger.info(
        "cap.archive_manage start run_id=%s op=%s format=%s n_sources=%d",
        ctx.run_id,
        op,
        fmt,
        len(sources or []),
    )

    with tempfile.TemporaryDirectory(prefix="aakaar-archive-") as tmp:
        tmp_path = Path(tmp)
        if op == "create":
            return _do_create(ctx, sources, str(fmt), tmp_path, zipfile, tarfile)

        raw = ctx.object_store.get(archive)
        resolved_fmt = fmt or _sniff_format(raw, archive)
        if op == "list":
            entries = _read_entries(raw, resolved_fmt, tmp_path, zipfile, tarfile)
            logger.info(
                "cap.archive_manage list ok run_id=%s format=%s entries=%d",
                ctx.run_id,
                resolved_fmt,
                len(entries),
            )
            return {
                "op": op,
                "format": resolved_fmt,
                "entries": entries,
                "extracted_uris": [],
                "archive_uri": None,
            }
        # extract
        return _do_extract(ctx, raw, resolved_fmt, tmp_path, zipfile, tarfile)


def _do_create(
    ctx: ActivityContext,
    sources: list[str],
    fmt: str,
    tmp_path: Path,
    zipfile: Any,
    tarfile: Any,
) -> dict[str, Any]:
    names = _dedupe_names([_basename_for_uri(s) for s in sources])
    archive_path = tmp_path / f"archive.{fmt.replace('.', '_')}"

    if fmt == "zip":
        with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for src_uri, arcname in zip(sources, names, strict=True):
                zf.writestr(arcname, ctx.object_store.get(src_uri))
    else:
        mode = _TAR_WRITE_MODE[fmt]
        with tarfile.open(archive_path, mode) as tf:
            for src_uri, arcname in zip(sources, names, strict=True):
                data = ctx.object_store.get(src_uri)
                member_file = tmp_path / f"_stage_{uuid.uuid4().hex}"
                member_file.write_bytes(data)
                tf.add(member_file, arcname=arcname)
                member_file.unlink(missing_ok=True)

    out_bytes = archive_path.read_bytes()
    ext = "tgz" if fmt == "tar.gz" else fmt
    key = f"runs/{ctx.run_id}/archives/{uuid.uuid4().hex}.{ext}"
    obj = ctx.object_store.put(str(ctx.tenant_id), key, out_bytes)
    logger.info(
        "cap.archive_manage create ok run_id=%s format=%s entries=%d uri=%s bytes=%d",
        ctx.run_id,
        fmt,
        len(names),
        obj.uri,
        len(out_bytes),
    )
    return {
        "op": "create",
        "format": fmt,
        "archive_uri": obj.uri,
        "entries": [],
        "extracted_uris": [],
    }


def _read_entries(
    raw: bytes, fmt: str, tmp_path: Path, zipfile: Any, tarfile: Any
) -> list[dict[str, Any]]:
    archive_path = tmp_path / "in_archive"
    archive_path.write_bytes(raw)
    entries: list[dict[str, Any]] = []
    if fmt == "zip":
        with zipfile.ZipFile(archive_path, "r") as zf:
            for info in zf.infolist():
                entries.append(
                    {
                        "name": info.filename,
                        "size": int(info.file_size),
                        "is_dir": info.is_dir(),
                    }
                )
    else:
        mode = "r:gz" if fmt == "tar.gz" else "r:*"
        with tarfile.open(archive_path, mode) as tf:
            for member in tf.getmembers():
                entries.append(
                    {
                        "name": member.name,
                        "size": int(member.size),
                        "is_dir": member.isdir(),
                    }
                )
    return entries


def _do_extract(
    ctx: ActivityContext,
    raw: bytes,
    fmt: str,
    tmp_path: Path,
    zipfile: Any,
    tarfile: Any,
) -> dict[str, Any]:
    archive_path = tmp_path / "in_archive"
    archive_path.write_bytes(raw)

    run_prefix = f"runs/{ctx.run_id}/extracted/{uuid.uuid4().hex}"
    entries: list[dict[str, Any]] = []
    extracted_uris: list[str] = []

    if fmt == "zip":
        with zipfile.ZipFile(archive_path, "r") as zf:
            for info in zf.infolist():
                is_dir = info.is_dir()
                entries.append(
                    {
                        "name": info.filename,
                        "size": int(info.file_size),
                        "is_dir": is_dir,
                    }
                )
                if is_dir:
                    continue
                if _is_unsafe_member(info.filename):
                    raise RuntimeError(
                        f"cap.archive_manage: refusing unsafe archive member "
                        f"{info.filename!r}"
                    )
                data = zf.read(info)
                uri = _store_member(ctx, run_prefix, info.filename, data)
                extracted_uris.append(uri)
    else:
        mode = "r:gz" if fmt == "tar.gz" else "r:*"
        with tarfile.open(archive_path, mode) as tf:
            for member in tf.getmembers():
                entries.append(
                    {
                        "name": member.name,
                        "size": int(member.size),
                        "is_dir": member.isdir(),
                    }
                )
                if member.isdir():
                    continue
                if not member.isreg():
                    # Skip symlinks, hardlinks, devices, fifos — never extracted.
                    raise RuntimeError(
                        f"cap.archive_manage: refusing non-regular archive member "
                        f"{member.name!r}"
                    )
                if _is_unsafe_member(member.name):
                    raise RuntimeError(
                        f"cap.archive_manage: refusing unsafe archive member "
                        f"{member.name!r}"
                    )
                fh = tf.extractfile(member)
                data = fh.read() if fh is not None else b""
                uri = _store_member(ctx, run_prefix, member.name, data)
                extracted_uris.append(uri)

    logger.info(
        "cap.archive_manage extract ok run_id=%s format=%s files=%d",
        ctx.run_id,
        fmt,
        len(extracted_uris),
    )
    return {
        "op": "extract",
        "format": fmt,
        "extracted_uris": extracted_uris,
        "entries": entries,
        "archive_uri": None,
    }


def _store_member(
    ctx: ActivityContext, run_prefix: str, member_name: str, data: bytes
) -> str:
    """Write one extracted member into object storage under the run prefix.

    `member_name` is sanitized to a forward-slash relative key (the object
    store also rejects traversal, so this is belt-and-suspenders).
    """
    rel = member_name.replace("\\", "/").lstrip("/")
    rel = posixpath.normpath(rel)
    if rel in (".", "..") or rel.startswith("../"):
        raise RuntimeError(
            f"cap.archive_manage: refusing unsafe archive member {member_name!r}"
        )
    key = f"{run_prefix}/{rel}"
    obj = ctx.object_store.put(str(ctx.tenant_id), key, data)
    return obj.uri


# Keep `os` referenced for any future host-side stat needs without a lint warning.
_ = os
