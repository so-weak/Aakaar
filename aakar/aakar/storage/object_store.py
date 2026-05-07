"""Object storage abstraction.

v1 ships only a local-filesystem driver. The interface is shaped so an S3
driver can drop in later without callers changing.

URIs are scheme-prefixed strings:
  - aakar://t/{tenant_id}/{key}     — managed (tenant-scoped) objects

The driver translates aakar:// URIs to concrete paths internally. Callers
never see the on-disk path; that keeps the swap to S3 mechanical.

Tenant isolation is structural: every operation requires a `tenant_id` and
the driver refuses to read/write keys that resolve outside the tenant root.
"""

from __future__ import annotations

import hashlib
import os
import shutil
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


URI_SCHEME = "aakar"
URI_PREFIX = f"{URI_SCHEME}://t/"


class ObjectStoreError(Exception):
    """Base class for object-store errors."""


class ObjectNotFound(ObjectStoreError):
    """Lookup of a missing tenant-scoped object."""


@dataclass(frozen=True, slots=True)
class StoredObject:
    """Pointer to a stored object. `uri` is canonical; `size` and `sha256` are
    set by the driver at write time."""

    uri: str
    tenant_id: str
    key: str
    size: int
    sha256: str


class ObjectStorage(Protocol):
    """Tenant-scoped binary object storage.

    All keys are interpreted relative to the tenant — there is no cross-tenant
    addressing. URIs returned by `put` round-trip through `get` and `stat`.
    """

    def put(self, tenant_id: str, key: str, data: bytes) -> StoredObject: ...

    def put_file(self, tenant_id: str, key: str, source_path: Path) -> StoredObject: ...

    def get(self, uri: str) -> bytes: ...

    def open_read(self, uri: str) -> Iterator[bytes]: ...

    def stat(self, uri: str) -> StoredObject: ...

    def delete(self, uri: str) -> None: ...

    def list(self, tenant_id: str, prefix: str = "") -> list[StoredObject]: ...


# ---------- helpers --------------------------------------------------------


def make_uri(tenant_id: str, key: str) -> str:
    return f"{URI_PREFIX}{tenant_id}/{_normalize_key(key)}"


def parse_uri(uri: str) -> tuple[str, str]:
    """Return (tenant_id, key). Raises ValueError on malformed URIs."""
    if not uri.startswith(URI_PREFIX):
        raise ValueError(f"not a managed-storage URI: {uri!r}")
    rest = uri[len(URI_PREFIX) :]
    if "/" not in rest:
        raise ValueError(f"managed-storage URI missing key: {uri!r}")
    tenant_id, key = rest.split("/", 1)
    if not tenant_id:
        raise ValueError(f"managed-storage URI missing tenant: {uri!r}")
    return tenant_id, _normalize_key(key)


def _normalize_key(key: str) -> str:
    """Reject path-traversal and absolute keys. Keys are forward-slash-separated
    relative paths within the tenant root."""
    if not key:
        raise ValueError("object key must be non-empty")
    if key.startswith("/"):
        raise ValueError(f"object key must be relative: {key!r}")
    parts = key.split("/")
    for p in parts:
        if p in ("", ".", ".."):
            raise ValueError(f"object key has invalid segment: {key!r}")
    return "/".join(parts)


# ---------- LocalFs driver -------------------------------------------------


class LocalFsObjectStore:
    """Filesystem-backed object store.

    Layout:
        {root}/tenants/{tenant_id}/{key}

    The driver eagerly creates per-tenant directories. Cross-tenant access is
    blocked structurally — every entry point requires a tenant_id (or a URI
    that contains one) and the resolved path is checked to be within the
    tenant root before any I/O.
    """

    def __init__(self, root: Path | str) -> None:
        self._root = Path(root).resolve()
        self._root.mkdir(parents=True, exist_ok=True)

    @property
    def root(self) -> Path:
        return self._root

    # --- writes ------------------------------------------------------------

    def put(self, tenant_id: str, key: str, data: bytes) -> StoredObject:
        path = self._resolve(tenant_id, key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return StoredObject(
            uri=make_uri(tenant_id, key),
            tenant_id=tenant_id,
            key=_normalize_key(key),
            size=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
        )

    def put_file(self, tenant_id: str, key: str, source_path: Path) -> StoredObject:
        src = Path(source_path)
        if not src.is_file():
            raise ObjectStoreError(f"source is not a file: {src}")
        path = self._resolve(tenant_id, key)
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, path)
        h = hashlib.sha256()
        size = 0
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
                size += len(chunk)
        return StoredObject(
            uri=make_uri(tenant_id, key),
            tenant_id=tenant_id,
            key=_normalize_key(key),
            size=size,
            sha256=h.hexdigest(),
        )

    # --- reads -------------------------------------------------------------

    def get(self, uri: str) -> bytes:
        tenant_id, key = parse_uri(uri)
        path = self._resolve(tenant_id, key)
        if not path.is_file():
            raise ObjectNotFound(uri)
        return path.read_bytes()

    def open_read(self, uri: str) -> Iterator[bytes]:
        tenant_id, key = parse_uri(uri)
        path = self._resolve(tenant_id, key)
        if not path.is_file():
            raise ObjectNotFound(uri)
        with path.open("rb") as f:
            yield from iter(lambda: f.read(65536), b"")

    def stat(self, uri: str) -> StoredObject:
        tenant_id, key = parse_uri(uri)
        path = self._resolve(tenant_id, key)
        if not path.is_file():
            raise ObjectNotFound(uri)
        h = hashlib.sha256()
        size = 0
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
                size += len(chunk)
        return StoredObject(
            uri=uri, tenant_id=tenant_id, key=key, size=size, sha256=h.hexdigest()
        )

    def delete(self, uri: str) -> None:
        tenant_id, key = parse_uri(uri)
        path = self._resolve(tenant_id, key)
        if not path.is_file():
            raise ObjectNotFound(uri)
        path.unlink()

    def list(self, tenant_id: str, prefix: str = "") -> list[StoredObject]:
        base = self._tenant_root(tenant_id)
        if not base.is_dir():
            return []
        out: list[StoredObject] = []
        for p in sorted(base.rglob("*")):
            if not p.is_file():
                continue
            rel = p.relative_to(base).as_posix()
            if prefix and not rel.startswith(prefix):
                continue
            stat = p.stat()
            out.append(
                StoredObject(
                    uri=make_uri(tenant_id, rel),
                    tenant_id=tenant_id,
                    key=rel,
                    size=stat.st_size,
                    sha256="",  # cheap listing; callers can stat() for the digest
                )
            )
        return out

    # --- internals ---------------------------------------------------------

    def _tenant_root(self, tenant_id: str) -> Path:
        if not tenant_id or "/" in tenant_id or tenant_id in (".", ".."):
            raise ValueError(f"invalid tenant_id: {tenant_id!r}")
        return self._root / "tenants" / tenant_id

    def _resolve(self, tenant_id: str, key: str) -> Path:
        base = self._tenant_root(tenant_id)
        path = (base / _normalize_key(key)).resolve()
        # Defense-in-depth: resolved path must remain within the tenant root.
        try:
            path.relative_to(base.resolve())
        except ValueError as e:
            raise ObjectStoreError(
                f"resolved path escapes tenant root: tenant={tenant_id} key={key!r}"
            ) from e
        return path


# Avoid an unused-import lint on os (kept for future stat fields if needed).
_ = os
