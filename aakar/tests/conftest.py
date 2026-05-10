"""Shared fixtures for API tests.

Each test gets a fresh SQLite DB, fresh filesystem dirs, a fresh fake LLM,
and a fully-wired FastAPI app. Because the planner depends on a `LLMClient`
Protocol, tests can prime its replies through the `fake_llm` fixture.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aakar.api import AppDependencies, create_app
from aakar.api.config import Settings
from aakar.db.models import Base
from aakar.db.session import EngineConfig, SessionFactory, make_engine
from aakar.planner import FakeEmbeddingsClient, FakeLLMClient
from aakar.shared.registry import build_default_registry
from aakar.storage import FaissVectorStore, LocalFsObjectStore
from aakar.vault import LocalVault
from aakar.workers.browser import FakeBrowserPool


@pytest.fixture()
def fake_llm() -> FakeLLMClient:
    return FakeLLMClient()


@pytest.fixture()
def fake_browser_pool() -> FakeBrowserPool:
    return FakeBrowserPool()


@pytest.fixture()
def deps(
    tmp_path: Path, fake_llm: FakeLLMClient, fake_browser_pool: FakeBrowserPool
) -> Iterator[AppDependencies]:
    db_path = tmp_path / "aakar.sqlite"
    engine = make_engine(EngineConfig(url=f"sqlite:///{db_path}"))
    Base.metadata.create_all(engine)

    embeddings = FakeEmbeddingsClient(dim_=16)
    vector_store = FaissVectorStore(tmp_path / "vector", dim=embeddings.dim)
    object_store = LocalFsObjectStore(tmp_path / "objects")
    vault = LocalVault(tmp_path / "vault")

    deps = AppDependencies(
        settings=Settings(
            db_url=f"sqlite:///{db_path}",
            data_dir=tmp_path,
            jwt_secret="test-secret-must-be-long-enough-for-hs256",
            access_token_ttl_minutes=60,
            embeddings_dim=embeddings.dim,
            # Tests assert on exact session-call sequences and event lists
            # — leave the live-screenshot capture off so the executor's
            # post-node hook doesn't inject extra screenshot() calls or
            # extra LIVE_SCREEN events. Tests that exercise the feature
            # opt in explicitly.
            live_screenshots=False,
        ),
        engine=engine,
        session_factory=SessionFactory(engine),
        registry=build_default_registry(),
        llm=fake_llm,
        embeddings=embeddings,
        vector_store=vector_store,
        object_store=object_store,
        vault=vault,
        browser_pool=fake_browser_pool,
    )
    try:
        yield deps
    finally:
        engine.dispose()


@pytest.fixture()
def app(deps: AppDependencies) -> FastAPI:
    return create_app(deps)


@pytest.fixture()
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c
