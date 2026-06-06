"""Hugging Face implementation of the Embeddings protocol.

Loads a BAAI/bge-* model with `sentence-transformers` and runs inference
locally instead of calling a remote embeddings API. Two layers of caching:

1. Disk: Hugging Face caches downloaded weights under `cache_folder`
   (defaults to `~/.cache/huggingface/hub`), so repeat process starts
   skip the download.
2. Process: the loaded `SentenceTransformer` instance is kept in a
   module-level dict keyed by (model_name, cache_folder, device), so
   constructing a second `BGEEmbeddingsClient` with the same config
   reuses the already-loaded weights and tokenizer instead of paying
   the multi-second load cost again.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Sequence
from dataclasses import dataclass, field
from threading import Lock

from sentence_transformers import SentenceTransformer

from aakaar.planner.embeddings import EmbeddingsClient

logger = logging.getLogger(__name__)

_DEFAULT_BGE_MODEL = "BAAI/bge-small-en-v1.5"


_model_cache: dict[tuple[str, str | None, str | None, bool], SentenceTransformer] = {}
_cache_lock = Lock()


def _enforce_offline() -> None:
    """Forbid any Hugging Face hub access for the rest of the process.

    Set before constructing the model so sentence-transformers /
    huggingface_hub load strictly from the local cache. This is what makes
    the embedder safe on the airgapped target.
    """
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"


def _load_model(
    model_name: str,
    cache_folder: str | None,
    device: str | None,
    local_files_only: bool,
) -> SentenceTransformer:
    key = (model_name, cache_folder, device, local_files_only)
    with _cache_lock:
        m = _model_cache.get(key)
        if m is None:
            if local_files_only:
                _enforce_offline()
                logger.info(
                    "embeddings: loading %s offline (local cache only)", model_name
                )
            m = SentenceTransformer(
                model_name,
                cache_folder=cache_folder,
                device=device,
                local_files_only=local_files_only,
            )
            _model_cache[key] = m
    return m


@dataclass
class BGEEmbeddingsClient(EmbeddingsClient):
    model_name: str = _DEFAULT_BGE_MODEL
    cache_folder: str | None = None
    device: str | None = None
    normalize: bool = True
    batch_size: int = 32
    local_files_only: bool = False
    _model: SentenceTransformer = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._model = _load_model(
            self.model_name, self.cache_folder, self.device, self.local_files_only
        )

    @property
    def dim(self) -> int:
        fn = getattr(self._model, "get_embedding_dimension", None)
        if fn is None:
            fn = self._model.get_sentence_embedding_dimension
        return int(fn())

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        vectors = self._model.encode(
            list(texts),
            batch_size=self.batch_size,
            normalize_embeddings=self.normalize,
            convert_to_numpy=True,
            show_progress_bar=False,
        )
        return [v.tolist() for v in vectors]
