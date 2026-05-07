"""Tests for the faiss vector store."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from aakar.storage import FaissVectorStore, VectorItem


def _vec(*xs: float) -> list[float]:
    return list(xs)


def test_upsert_and_search(tmp_path: Path) -> None:
    store = FaissVectorStore(tmp_path, dim=3)
    items = [
        VectorItem(id="a", vector=_vec(1, 0, 0), payload={"name": "alpha"}),
        VectorItem(id="b", vector=_vec(0, 1, 0), payload={"name": "beta"}),
        VectorItem(id="c", vector=_vec(0, 0, 1), payload={"name": "gamma"}),
    ]
    store.upsert("t1", "caps", items)
    assert store.count("t1", "caps") == 3

    hits = store.search("t1", "caps", _vec(0.9, 0.1, 0), k=2)
    assert len(hits) == 2
    assert hits[0].id == "a"
    assert hits[0].payload["name"] == "alpha"


def test_upsert_replaces_same_id(tmp_path: Path) -> None:
    store = FaissVectorStore(tmp_path, dim=3)
    store.upsert("t1", "caps", [VectorItem(id="a", vector=_vec(1, 0, 0), payload={"v": 1})])
    store.upsert("t1", "caps", [VectorItem(id="a", vector=_vec(0, 1, 0), payload={"v": 2})])
    assert store.count("t1", "caps") == 1
    hits = store.search("t1", "caps", _vec(0, 1, 0), k=1)
    assert hits[0].id == "a"
    assert hits[0].payload["v"] == 2


def test_delete(tmp_path: Path) -> None:
    store = FaissVectorStore(tmp_path, dim=2)
    store.upsert(
        "t1",
        "caps",
        [
            VectorItem(id="a", vector=_vec(1, 0)),
            VectorItem(id="b", vector=_vec(0, 1)),
        ],
    )
    store.delete("t1", "caps", ["a"])
    assert store.count("t1", "caps") == 1
    hits = store.search("t1", "caps", _vec(1, 0), k=5)
    assert {h.id for h in hits} == {"b"}


def test_persists_across_instances(tmp_path: Path) -> None:
    store1 = FaissVectorStore(tmp_path, dim=2)
    store1.upsert("t1", "caps", [VectorItem(id="a", vector=_vec(1, 0), payload={"x": 1})])

    store2 = FaissVectorStore(tmp_path, dim=2)
    hits = store2.search("t1", "caps", _vec(1, 0), k=1)
    assert hits[0].id == "a"
    assert hits[0].payload == {"x": 1}


def test_tenant_isolation(tmp_path: Path) -> None:
    store = FaissVectorStore(tmp_path, dim=2)
    store.upsert("ta", "caps", [VectorItem(id="x", vector=_vec(1, 0))])
    store.upsert("tb", "caps", [VectorItem(id="x", vector=_vec(0, 1))])
    assert store.count("ta", "caps") == 1
    assert store.count("tb", "caps") == 1


def test_dim_mismatch_rejected(tmp_path: Path) -> None:
    store = FaissVectorStore(tmp_path, dim=3)
    with pytest.raises(ValueError):
        store.upsert("t1", "caps", [VectorItem(id="a", vector=_vec(1, 0))])


def test_empty_search_on_empty_index(tmp_path: Path) -> None:
    store = FaissVectorStore(tmp_path, dim=2)
    assert store.search("t1", "caps", _vec(1, 0)) == []


def test_numpy_input_accepted(tmp_path: Path) -> None:
    store = FaissVectorStore(tmp_path, dim=2)
    store.upsert("t1", "caps", [VectorItem(id="a", vector=np.array([1.0, 0.0]))])
    hits = store.search("t1", "caps", np.array([1.0, 0.0]), k=1)
    assert hits[0].id == "a"
