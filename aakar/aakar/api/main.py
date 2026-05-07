"""Default ASGI entrypoint for local/dev API runs."""

from __future__ import annotations

from openai import OpenAI

from aakar.api import AppDependencies, create_app, load_settings
from aakar.db.session import EngineConfig, SessionFactory, make_engine
from aakar.planner import FakeEmbeddingsClient, FakeLLMClient
from aakar.planner.hf_impl import BGEEmbeddingsClient
from aakar.planner.openai_impl import OpenAILLMClient
from aakar.shared.registry import build_default_registry
from aakar.storage import FaissVectorStore, LocalFsObjectStore
from aakar.vault import LocalVault
from aakar.workers.browser.playwright import PlaywrightBrowserPool


def build_app() -> object:
    settings = load_settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)

    engine = make_engine(EngineConfig(url=settings.db_url))

    if settings.openai_api_key:
        if settings.openai_base_url:
            openai_client = OpenAI(api_key=settings.openai_api_key, base_url=settings.openai_base_url)
        else:
            openai_client = OpenAI(api_key=settings.openai_api_key)
        llm = OpenAILLMClient(openai_client, model=settings.llm_model)
        embeddings = BGEEmbeddingsClient(
            model_name=settings.embedding_model,
            cache_folder=str(settings.data_dir / "hf_cache"),
        )
    else:
        llm = FakeLLMClient()
        embeddings = FakeEmbeddingsClient(dim_=settings.embeddings_dim)

    vector_store = FaissVectorStore(settings.data_dir / "vector", dim=embeddings.dim)
    object_store = LocalFsObjectStore(settings.data_dir / "objects")
    vault = LocalVault(settings.data_dir)

    # Construction is cheap (Chromium isn't launched until the first
    # checkout), so we always wire the pool unless explicitly disabled.
    # The lifespan hook tears it down on shutdown.
    if settings.browser_pool == "playwright":
        browser_pool = PlaywrightBrowserPool(headless=settings.browser_headless)
    else:
        browser_pool = None

    deps = AppDependencies(
        settings=settings,
        engine=engine,
        session_factory=SessionFactory(engine),
        registry=build_default_registry(),
        llm=llm,
        embeddings=embeddings,
        vector_store=vector_store,
        object_store=object_store,
        vault=vault,
        browser_pool=browser_pool,
    )
    return create_app(deps)


app = build_app()
