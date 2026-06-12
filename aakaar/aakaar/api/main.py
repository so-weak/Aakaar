"""Default ASGI entrypoint for local/dev API runs."""

from __future__ import annotations

import logging
import httpx

from openai import OpenAI

from aakaar.api import AppDependencies, create_app, load_settings
from aakaar.core.logging import setup_logging
from aakaar.db.session import EngineConfig, SessionFactory, make_engine
from aakaar.planner import FakeEmbeddingsClient, FakeLLMClient
from aakaar.planner.hf_impl import BGEEmbeddingsClient
from aakaar.planner.openai_impl import OpenAILLMClient
from aakaar.shared.registry import build_default_registry
from aakaar.storage import ChromaVectorStore, LocalFsObjectStore
from aakaar.vault import LocalVault
from aakaar.workers.browser.playwright import PlaywrightBrowserPool

logger = logging.getLogger(__name__)


def build_app() -> object:
    # Configure logging before anything else so import-time warnings from
    # downstream modules (e.g. chromadb, sentence_transformers) flow through
    # our handler instead of Python's default lastResort.
    setup_logging()
    logger.info("aakaar API: building app")
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

    engine = make_engine(EngineConfig(url=settings.db_url, rls_strict=settings.rls_strict))

    if settings.openai_api_key:
        if settings.openai_base_url:
            openai_client = OpenAI(api_key=settings.openai_api_key, base_url=settings.openai_base_url, http_client=httpx.Client(ssl_verify=True))
            logger.info("LLM: OpenAI client (base_url=%s, model=%s)", settings.openai_base_url, settings.llm_model)
        else:
            openai_client = OpenAI(api_key=settings.openai_api_key)
            logger.info("LLM: OpenAI client (default base_url, model=%s)", settings.llm_model)
        llm = OpenAILLMClient(openai_client, model=settings.llm_model)
        embeddings = BGEEmbeddingsClient(
            model_name=settings.embedding_model,
            cache_folder=str(settings.data_dir / "hf_cache"),
            local_files_only=settings.embeddings_offline,
        )
        logger.info(
            "embeddings: BGE model=%s cache=%s offline=%s",
            settings.embedding_model,
            settings.data_dir / "hf_cache",
            settings.embeddings_offline,
        )
    else:
        logger.warning(
            "OPENAI_API_KEY not set; falling back to FakeLLMClient/FakeEmbeddingsClient (dev/test only)"
        )
        llm = FakeLLMClient()
        embeddings = FakeEmbeddingsClient(dim_=settings.embeddings_dim)

    vector_store = ChromaVectorStore(settings.data_dir / "vector", dim=embeddings.dim)
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
        logger.info("browser pool: disabled (AAKAAR_BROWSER_POOL=%s)", settings.browser_pool)

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
    logger.info("aakaar API: app construction complete")
    return create_app(deps)


app = build_app()
