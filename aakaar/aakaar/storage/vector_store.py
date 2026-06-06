"""Vector store abstraction.

v1 driver: Chroma (chromadb) with a local persistent client, paired with the
SQLite primary database. Chroma keeps its own embedded store on local disk, so
the platform stays single-node and fully offline — no external vector service.

Indexes are partitioned by (tenant_id, namespace), one Chroma collection each.
Tenant isolation is structural — there is no cross-tenant query path.

Scoring: collections use cosine space. Chroma returns a cosine *distance*
(0 = identical); we convert to a similarity score `1 - distance` so that, as
with the old inner-product index, higher means more similar.

Embeddings are always supplied by the caller (BGE), so the collection is
created with `embedding_function=None`: Chroma never instantiates its bundled
ONNX embedder and therefore never reaches out to the network.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import threading
from dataclasses import dataclass, field
from hashlib import sha1
from pathlib import Path
from typing import Any, Protocol

import numpy as np

# Belt-and-suspenders: forbid Chroma's product telemetry. We also pass
# anonymized_telemetry=False per client. On an airgapped host the telemetry
# call fails before any network egress regardless, but we keep it off and
# silence its logger so it never clutters operator logs.
os.environ.setdefault("ANONYMIZED_TELEMETRY", "False")


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


# ---------- helpers --------------------------------------------------------


_PAYLOAD_KEY = "_payload"


def _to_floats(vector: list[float] | np.ndarray) -> list[float]:
    arr = np.asarray(vector, dtype=np.float32).ravel()
    return [float(x) for x in arr]


def _valid_segment(value: str, label: str) -> str:
    if not value or "/" in value or value in (".", ".."):
        raise ValueError(f"invalid {label}: {value!r}")
    return value


# ---------- Chroma driver --------------------------------------------------


class ChromaVectorStore:
    """Chroma-backed vector store, one collection per (tenant, namespace).

    All collections live in a single persistent Chroma store under
    ``{root}/chroma``. The driver is intended for embedded / single-node use.
    """

    def __init__(self, root: Path | str, dim: int) -> None:
        if dim <= 0:
            raise ValueError("dim must be positive")
        import chromadb
        from chromadb.config import Settings

        # chromadb 0.5.x still calls into posthog even with telemetry
        # disabled (the call fails on a version mismatch before sending), so
        # raise the telemetry logger above ERROR to keep logs clean.
        logging.getLogger("chromadb.telemetry").setLevel(logging.CRITICAL)
        self._root = Path(root).resolve()
        self._root.mkdir(parents=True, exist_ok=True)
        self._dim = dim
        self._lock = threading.RLock()
        self._client = chromadb.PersistentClient(
            path=str(self._root / "chroma"),
            settings=Settings(anonymized_telemetry=False, allow_reset=False),
        )
        self._collections: dict[str, Any] = {}

    def close(self) -> None:
        """Release the underlying Chroma system (and its SQLite connection).

        In the long-lived API process the store is created once and never
        closed; this is here mainly so tests that spin up many stores don't
        leak SQLite connections.
        """
        self._collections.clear()
        with contextlib.suppress(Exception):  # pragma: no cover - defensive teardown
            self._client.clear_system_cache()

    @property
    def root(self) -> Path:
        return self._root

    @property
    def dim(self) -> int:
        return self._dim

    def upsert(self, tenant_id: str, namespace: str, items: list[VectorItem]) -> None:
        if not items:
            return
        ids: list[str] = []
        embeddings: list[list[float]] = []
        metadatas: list[dict[str, Any]] = []
        for item in items:
            vec = _to_floats(item.vector)
            if len(vec) != self._dim:
                raise ValueError(f"expected dim {self._dim} vectors, got {len(vec)}")
            ids.append(item.id)
            embeddings.append(vec)
            metadatas.append({_PAYLOAD_KEY: json.dumps(item.payload, ensure_ascii=False)})
        with self._lock:
            col = self._collection(tenant_id, namespace)
            col.upsert(ids=ids, embeddings=embeddings, metadatas=metadatas)

    def search(
        self,
        tenant_id: str,
        namespace: str,
        query: list[float] | np.ndarray,
        k: int = 10,
    ) -> list[VectorHit]:
        if k <= 0:
            return []
        vec = _to_floats(query)
        if len(vec) != self._dim:
            raise ValueError(f"expected dim {self._dim} query, got {len(vec)}")
        with self._lock:
            col = self._collection(tenant_id, namespace)
            total = col.count()
            if total == 0:
                return []
            res = col.query(
                query_embeddings=[vec],
                n_results=min(k, total),
                include=["distances", "metadatas"],
            )
        ids = (res.get("ids") or [[]])[0]
        dists = (res.get("distances") or [[]])[0]
        metas = (res.get("metadatas") or [[]])[0]
        out: list[VectorHit] = []
        for sid, dist, meta in zip(ids, dists, metas, strict=False):
            payload_raw = (meta or {}).get(_PAYLOAD_KEY, "{}")
            try:
                payload = json.loads(payload_raw)
            except (TypeError, ValueError):
                payload = {}
            out.append(VectorHit(id=sid, score=1.0 - float(dist), payload=payload))
        return out

    def delete(self, tenant_id: str, namespace: str, ids: list[str]) -> None:
        if not ids:
            return
        with self._lock:
            col = self._collection(tenant_id, namespace)
            col.delete(ids=ids)

    def count(self, tenant_id: str, namespace: str) -> int:
        with self._lock:
            return int(self._collection(tenant_id, namespace).count())

    # --- internals ---------------------------------------------------------

    def _collection(self, tenant_id: str, namespace: str) -> Any:
        name = self._collection_name(tenant_id, namespace)
        col = self._collections.get(name)
        if col is None:
            col = self._client.get_or_create_collection(
                name=name,
                metadata={"hnsw:space": "cosine"},
                embedding_function=None,
            )
            self._collections[name] = col
        return col

    def _collection_name(self, tenant_id: str, namespace: str) -> str:
        _valid_segment(tenant_id, "tenant_id")
        _valid_segment(namespace, "namespace")
        # Chroma collection names: 3-63 chars, [a-zA-Z0-9._-], start/end
        # alphanumeric, no `..`. Sanitize then guarantee bounds with a hash
        # suffix so distinct (tenant, namespace) pairs never collide.
        raw = f"{tenant_id}__{namespace}"
        safe = re.sub(r"[^a-zA-Z0-9_-]", "-", raw)
        digest = sha1(raw.encode("utf-8")).hexdigest()[:10]
        name = f"v-{safe}-{digest}"
        return name[:63]
