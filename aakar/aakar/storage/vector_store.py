"""Vector store abstraction.

v1 driver: faiss on local disk (paired with SQLite). A pgvector driver lands
later for the Yugabyte path. The interface is intentionally narrow so both
backends can satisfy it.

Indexes are partitioned by (tenant_id, namespace). Tenant isolation is
structural — there is no cross-tenant query path.

Scoring: vectors are L2-normalized on write and at query time, so an inner-
product index returns cosine similarity in [-1, 1]. Higher is more similar.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import numpy as np


@dataclass(frozen=True, slots=True)
class VectorItem:
    """An item to upsert. `id` is caller-defined and must be stable across
    re-embeddings of the same logical entity (e.g. a capability ref)."""

    id: str
    vector: list[float] | np.ndarray
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class VectorHit:
    id: str
    score: float
    payload: dict[str, Any]


class VectorStore(Protocol):
    """Tenant-scoped vector index."""

    def upsert(self, tenant_id: str, namespace: str, items: list[VectorItem]) -> None: ...

    def search(
        self,
        tenant_id: str,
        namespace: str,
        query: list[float] | np.ndarray,
        k: int = 10,
    ) -> list[VectorHit]: ...

    def delete(self, tenant_id: str, namespace: str, ids: list[str]) -> None: ...

    def count(self, tenant_id: str, namespace: str) -> int: ...


# ---------- Faiss driver ---------------------------------------------------


class FaissVectorStore:
    """On-disk faiss-backed vector store, one index per (tenant, namespace).

    Each index lives at:
        {root}/vectors/{tenant_id}/{namespace}/index.faiss
        {root}/vectors/{tenant_id}/{namespace}/meta.json

    `meta.json` carries the string-id → int-id mapping plus per-id payload.
    The driver is intended for embedded / single-node use; concurrent access
    from multiple processes will corrupt indexes.
    """

    def __init__(self, root: Path | str, dim: int) -> None:
        if dim <= 0:
            raise ValueError("dim must be positive")
        self._root = Path(root).resolve()
        self._root.mkdir(parents=True, exist_ok=True)
        self._dim = dim
        self._lock = threading.RLock()

    @property
    def root(self) -> Path:
        return self._root

    @property
    def dim(self) -> int:
        return self._dim

    def upsert(self, tenant_id: str, namespace: str, items: list[VectorItem]) -> None:
        if not items:
            return
        with self._lock:
            shard = self._load(tenant_id, namespace)
            self._upsert_into(shard, items)
            self._save(tenant_id, namespace, shard)

    def search(
        self,
        tenant_id: str,
        namespace: str,
        query: list[float] | np.ndarray,
        k: int = 10,
    ) -> list[VectorHit]:
        if k <= 0:
            return []
        with self._lock:
            shard = self._load(tenant_id, namespace)
        if shard.index.ntotal == 0:
            return []
        q = self._prepare_vectors(np.asarray([query], dtype=np.float32))
        scores, ids = shard.index.search(q, min(k, shard.index.ntotal))
        out: list[VectorHit] = []
        for score, int_id in zip(scores[0], ids[0]):
            if int_id < 0:
                continue
            entry = shard.by_int.get(int(int_id))
            if entry is None:
                continue
            out.append(VectorHit(id=entry["string_id"], score=float(score), payload=entry["payload"]))
        return out

    def delete(self, tenant_id: str, namespace: str, ids: list[str]) -> None:
        if not ids:
            return
        with self._lock:
            shard = self._load(tenant_id, namespace)
            int_ids = [shard.by_str[s]["int_id"] for s in ids if s in shard.by_str]
            if not int_ids:
                return
            shard.index.remove_ids(np.asarray(int_ids, dtype=np.int64))
            for s in ids:
                entry = shard.by_str.pop(s, None)
                if entry is not None:
                    shard.by_int.pop(entry["int_id"], None)
            self._save(tenant_id, namespace, shard)

    def count(self, tenant_id: str, namespace: str) -> int:
        with self._lock:
            shard = self._load(tenant_id, namespace)
        return int(shard.index.ntotal)

    # --- internals ---------------------------------------------------------

    def _shard_dir(self, tenant_id: str, namespace: str) -> Path:
        if not tenant_id or "/" in tenant_id or tenant_id in (".", ".."):
            raise ValueError(f"invalid tenant_id: {tenant_id!r}")
        if not namespace or "/" in namespace or namespace in (".", ".."):
            raise ValueError(f"invalid namespace: {namespace!r}")
        return self._root / "vectors" / tenant_id / namespace

    def _load(self, tenant_id: str, namespace: str) -> _Shard:
        import faiss  # imported lazily so the module still imports without faiss installed

        d = self._shard_dir(tenant_id, namespace)
        d.mkdir(parents=True, exist_ok=True)
        idx_path = d / "index.faiss"
        meta_path = d / "meta.json"

        if idx_path.is_file() and meta_path.is_file():
            index = faiss.read_index(str(idx_path))
            if index.d != self._dim:
                raise ValueError(
                    f"existing index at {d} has dim {index.d}, store configured for {self._dim}"
                )
            with meta_path.open() as f:
                raw = json.load(f)
            by_str = {k: {"int_id": v["int_id"], "payload": v["payload"]} for k, v in raw["by_str"].items()}
            by_int = {int(e["int_id"]): {"string_id": k, "payload": e["payload"]} for k, e in by_str.items()}
            next_int = int(raw.get("next_int_id", 0))
            return _Shard(index=index, by_str=by_str, by_int=by_int, next_int_id=next_int)

        base = faiss.IndexFlatIP(self._dim)
        index = faiss.IndexIDMap2(base)
        return _Shard(index=index, by_str={}, by_int={}, next_int_id=0)

    def _save(self, tenant_id: str, namespace: str, shard: _Shard) -> None:
        import faiss

        d = self._shard_dir(tenant_id, namespace)
        d.mkdir(parents=True, exist_ok=True)
        idx_path = d / "index.faiss"
        meta_path = d / "meta.json"

        # Atomic-ish writes: write to .tmp then rename.
        tmp_idx = idx_path.with_suffix(".faiss.tmp")
        tmp_meta = meta_path.with_suffix(".json.tmp")
        faiss.write_index(shard.index, str(tmp_idx))
        with tmp_meta.open("w") as f:
            json.dump(
                {"by_str": shard.by_str, "next_int_id": shard.next_int_id},
                f,
                ensure_ascii=False,
            )
        tmp_idx.replace(idx_path)
        tmp_meta.replace(meta_path)

    def _upsert_into(self, shard: _Shard, items: list[VectorItem]) -> None:
        import faiss

        # Remove existing int ids for any string ids being re-upserted.
        replace_int_ids = [
            shard.by_str[i.id]["int_id"] for i in items if i.id in shard.by_str
        ]
        if replace_int_ids:
            shard.index.remove_ids(np.asarray(replace_int_ids, dtype=np.int64))

        new_int_ids: list[int] = []
        vectors: list[np.ndarray] = []
        for item in items:
            int_id = shard.next_int_id
            shard.next_int_id += 1
            new_int_ids.append(int_id)
            vectors.append(np.asarray(item.vector, dtype=np.float32))
            shard.by_str[item.id] = {"int_id": int_id, "payload": item.payload}
            shard.by_int[int_id] = {"string_id": item.id, "payload": item.payload}

        mat = self._prepare_vectors(np.stack(vectors, axis=0))
        ids_arr = np.asarray(new_int_ids, dtype=np.int64)
        shard.index.add_with_ids(mat, ids_arr)
        _ = faiss  # keep the import alive for the linter

    def _prepare_vectors(self, mat: np.ndarray) -> np.ndarray:
        if mat.ndim != 2 or mat.shape[1] != self._dim:
            raise ValueError(f"expected (n, {self._dim}) vectors, got {mat.shape}")
        if mat.dtype != np.float32:
            mat = mat.astype(np.float32)
        norms = np.linalg.norm(mat, axis=1, keepdims=True)
        norms = np.where(norms == 0, 1.0, norms)
        return mat / norms


@dataclass(slots=True)
class _Shard:
    """In-memory state for one (tenant, namespace) index."""

    index: Any  # faiss.IndexIDMap2 — typed as Any to avoid a hard import at module level
    by_str: dict[str, dict[str, Any]]  # string_id -> {int_id, payload}
    by_int: dict[int, dict[str, Any]]  # int_id -> {string_id, payload}
    next_int_id: int
