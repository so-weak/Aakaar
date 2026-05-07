"""Embeddings client abstraction.

Used to (a) embed capability descriptions for semantic search, and (b)
embed user messages to find relevant capabilities at planning time. Same
Protocol-and-fake pattern as the LLM client so tests don't hit OpenAI.

The embedding dimension is fixed per implementation — callers pair the
client with a `FaissVectorStore(dim=...)` of matching dimension. The
`dim` property exposes that value so wiring code can stay generic.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol

import numpy as np


class EmbeddingsClient(Protocol):
    """Minimal embeddings client. Returns one vector per input text."""

    @property
    def dim(self) -> int: ...

    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


# ---------- fake -----------------------------------------------------------


@dataclass
class FakeEmbeddingsClient:
    """Deterministic, content-addressed fake.

    Each text is hashed to a vector seeded by the hash, so the same text
    always produces the same embedding. Distinct texts get distinct vectors;
    semantically related texts are NOT close — this is fine for tests that
    just need stable, distinguishable embeddings.

    `dim` defaults to 16 to keep tests cheap.
    """

    dim_: int = 16
    calls: list[list[str]] = field(default_factory=list)

    @property
    def dim(self) -> int:
        return self.dim_

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        out: list[list[float]] = []
        for t in texts:
            seed = hash(t) & 0xFFFFFFFF
            rng = np.random.default_rng(seed)
            v = rng.standard_normal(self.dim_).astype(np.float32)
            v /= np.linalg.norm(v) or 1.0
            out.append(v.tolist())
        return out
