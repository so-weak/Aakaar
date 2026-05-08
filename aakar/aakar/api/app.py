"""FastAPI app factory.

`create_app(deps)` wires dependencies, mounts routers, installs error
handlers, and runs startup hooks (superuser bootstrap). Tests construct
their own AppDependencies with fakes; production wires the real
implementations in a separate module.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from aakar.api.bootstrap import bootstrap_superuser
from aakar.api.deps import AppDependencies
from aakar.api.errors import install_handlers
from aakar.api.routers import (
    admin as admin_router,
)
from aakar.api.routers import (
    auth as auth_router,
)
from aakar.api.routers import (
    capabilities as capabilities_router,
)
from aakar.api.routers import (
    chat as chat_router,
)
from aakar.api.routers import (
    chat_sessions as chat_sessions_router,
)
from aakar.api.routers import (
    objects as objects_router,
)
from aakar.api.routers import (
    runs as runs_router,
)
from aakar.api.routers import (
    stats as stats_router,
)
from aakar.api.routers import (
    superuser as superuser_router,
)
from aakar.api.routers import (
    workflows as workflows_router,
)


def create_app(deps: AppDependencies) -> FastAPI:
    @asynccontextmanager
    async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
        bootstrap_superuser(deps.settings, deps.session_factory)
        try:
            yield
        finally:
            # Tear down any pool that exposes async `shutdown()` — currently
            # PlaywrightBrowserPool. FakeBrowserPool (used in tests) doesn't
            # need this; the duck-typed check keeps test wiring untouched.
            shutdown = getattr(deps.browser_pool, "shutdown", None)
            if shutdown is not None:
                try:
                    await shutdown()
                except Exception:
                    pass

    app = FastAPI(title="Aakar", version="0.1.0", lifespan=_lifespan)
    app.state.deps = deps

    # The frontend SPA runs on a separate origin in dev (Vite at :5173, API at :8000).
    # In production both are typically served from the same host/origin and these
    # headers become a no-op. Allow-credentials is False because we use a Bearer
    # token in a header, not a cookie.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=deps.settings.cors_allow_origins,
        allow_methods=["*"],
        allow_headers=["*"],
        allow_credentials=False,
    )

    install_handlers(app)

    app.include_router(auth_router.router)
    app.include_router(superuser_router.router)
    app.include_router(admin_router.router)
    app.include_router(capabilities_router.router)
    app.include_router(workflows_router.router)
    app.include_router(runs_router.router)
    app.include_router(chat_router.router)
    app.include_router(chat_sessions_router.router)
    app.include_router(objects_router.router)
    app.include_router(stats_router.router)

    @app.get("/healthz")
    def _healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app


__all__ = ["AppDependencies", "create_app"]
