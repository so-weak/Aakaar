"""Tests for the capability semantic index."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel

from aakar.planner.capability_index import CapabilityIndex
from aakar.planner.embeddings import FakeEmbeddingsClient
from aakar.shared.registry import (
    CapabilityDefinition,
    Registry,
    build_default_registry,
)
from aakar.storage import FaissVectorStore


class _In(BaseModel):
    pass


class _Out(BaseModel):
    pass


def _add_cap(reg: Registry, ref: str, description: str, *tags: str) -> None:
    reg.add(
        CapabilityDefinition(
            ref=ref,
            description=description,
            input_schema=_In,
            output_schema=_Out,
            tags=tags,
        )
    )


def _make_index(tmp_path: Path) -> tuple[CapabilityIndex, Registry]:
    reg = build_default_registry()
    embeddings = FakeEmbeddingsClient(dim_=16)
    store = FaissVectorStore(tmp_path, dim=embeddings.dim)
    return CapabilityIndex(registry=reg, embeddings=embeddings, vector_store=store), reg


def test_search_returns_only_indexed(tmp_path: Path) -> None:
    idx, reg = _make_index(tmp_path)
    _add_cap(reg, "cap.hdfc_login", "Log into HDFC portal", "auth", "hdfc")
    _add_cap(reg, "cap.icici_login", "Log into ICICI portal", "auth", "icici")
    idx.reindex_for_tenant("t1", {"cap.hdfc_login"})

    refs = idx.search("t1", "log in to hdfc", k=5)
    assert refs == ["cap.hdfc_login"]


def test_revoke_removes_from_index(tmp_path: Path) -> None:
    idx, reg = _make_index(tmp_path)
    _add_cap(reg, "cap.x", "x")
    _add_cap(reg, "cap.y", "y")
    idx.reindex_for_tenant("t1", {"cap.x", "cap.y"})
    assert set(idx.search("t1", "x", k=5)) == {"cap.x", "cap.y"}

    idx.revoke_for_tenant("t1", ["cap.x"])
    assert idx.search("t1", "x", k=5) == ["cap.y"]


def test_unknown_ref_in_grants_is_silently_skipped(tmp_path: Path) -> None:
    idx, reg = _make_index(tmp_path)
    _add_cap(reg, "cap.real", "real")
    idx.reindex_for_tenant("t1", {"cap.real", "cap.does_not_exist"})
    refs = idx.search("t1", "real", k=5)
    assert refs == ["cap.real"]


def test_tenant_isolation(tmp_path: Path) -> None:
    idx, reg = _make_index(tmp_path)
    _add_cap(reg, "cap.shared", "shared")
    idx.reindex_for_tenant("ta", {"cap.shared"})
    assert idx.search("ta", "x", k=5) == ["cap.shared"]
    assert idx.search("tb", "x", k=5) == []
