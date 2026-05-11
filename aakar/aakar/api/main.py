"""Default ASGI entrypoint for local/dev API runs."""

from __future__ import annotations

import logging

from openai import OpenAI

from aakar.api import AppDependencies, create_app, load_settings
from aakar.db.session import EngineConfig, SessionFactory, make_engine
from aakar.planner import FakeEmbeddingsClient, FakeLLMClient
from aakar.planner.hf_impl import BGEEmbeddingsClient
from aakar.planner.openai_impl import OpenAILLMClient
from aakar.shared.logging_setup import setup_logging
from aakar.shared.registry import build_default_registry
from aakar.storage import FaissVectorStore, LocalFsObjectStore
from aakar.vault import LocalVault
from aakar.workers.browser.playwright import PlaywrightBrowserPool


logger = logging.getLogger(__name__)


def build_app() -> object:
    # Configure logging before anything else so import-time warnings from
    # downstream modules (e.g. faiss, sentence_transformers) flow through
    # our handler instead of Python's default lastResort.
    setup_logging()
    logger.info("aakar API: building app")
    settings = load_settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    logger.debug(
        "settings: data_dir=%s db_url=%s browser_pool=%s headless=%s llm_model=%s",
        settings.data_dir,
        settings.db_url,
        settings.browser_pool,
        settings.browser_headless,
        settings.llm_model,
    )

    engine = make_engine(EngineConfig(url=settings.db_url))

    if settings.openai_api_key:
        if settings.openai_base_url:
            openai_client = OpenAI(api_key=settings.openai_api_key, base_url=settings.openai_base_url)
            logger.info("LLM: OpenAI client (base_url=%s, model=%s)", settings.openai_base_url, settings.llm_model)
        else:
            openai_client = OpenAI(api_key=settings.openai_api_key)
            logger.info("LLM: OpenAI client (default base_url, model=%s)", settings.llm_model)
        llm = OpenAILLMClient(openai_client, model=settings.llm_model)
        embeddings = BGEEmbeddingsClient(
            model_name=settings.embedding_model,
            cache_folder=str(settings.data_dir / "hf_cache"),
        )
        logger.info("embeddings: BGE model=%s cache=%s", settings.embedding_model, settings.data_dir / "hf_cache")
    else:
        logger.warning(
            "OPENAI_API_KEY not set; falling back to FakeLLMClient/FakeEmbeddingsClient (dev/test only)"
        )
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
        logger.info("browser pool: playwright (headless=%s)", settings.browser_headless)
    else:
        browser_pool = None
        logger.info("browser pool: disabled (AAKAR_BROWSER_POOL=%s)", settings.browser_pool)

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
    logger.info("aakar API: app construction complete")
    return create_app(deps)


app = build_app()
