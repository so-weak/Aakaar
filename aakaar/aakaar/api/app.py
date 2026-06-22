"""FastAPI app factory.

`create_app(deps)` wires dependencies, mounts routers, installs error
handlers, and runs startup hooks (superuser bootstrap). Tests construct
their own AppDependencies with fakes; production wires the real
implementations in a separate module.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from aakaar.api.bootstrap import bootstrap_superuser
from aakaar.api.deps import AppDependencies
from aakaar.api.routers import (
    admin as admin_router,
)
from aakaar.api.routers import (
    agents as agents_router,
)
from aakaar.api.routers import (
    approvals as approvals_router,
)
from aakaar.api.routers import (
    audit as audit_router,
)
from aakaar.api.routers import (
    auth as auth_router,
)
from aakaar.api.routers import (
    capabilities as capabilities_router,
)
from aakaar.api.routers import (
    chat as chat_router,
)
from aakaar.api.routers import (
    chat_sessions as chat_sessions_router,
)
from aakaar.api.routers import (
    jwks as jwks_router,
)
from aakaar.api.routers import (
    mfa as mfa_router,
)
from aakaar.api.routers import (
    objects as objects_router,
)
from aakaar.api.routers import (
    oidc as oidc_router,
)
from aakaar.api.routers import (
    recordings as recordings_router,
)
from aakaar.api.routers import (
    retention as retention_router,
)
from aakaar.api.routers import (
    runs as runs_router,
)
from aakaar.api.routers import (
    schedules as schedules_router,
)
from aakaar.api.routers import (
    stats as stats_router,
)
from aakaar.api.routers import (
    superuser as superuser_router,
)
from aakaar.api.routers import (
    workflows as workflows_router,
)
from aakaar.api.routers import (
    ws as ws_router,
)
from aakaar.core.errors import install_handlers
from aakaar.core.middleware.metrics import MetricsMiddleware, metrics_response
from aakaar.core.middleware.rate_limit import RateLimitMiddleware, TokenBucketLimiter
from aakaar.core.middleware.request_id import RequestIdMiddleware
from aakaar.services.recordings import RecordingService

if TYPE_CHECKING:
    from aakaar.workers.remote.broker_link import BrokerLink

logger = logging.getLogger(__name__)


def _build_broker_link(deps: AppDependencies) -> BrokerLink | None:
    """Optional master link to a rendezvous broker (see aakaar-broker/).

    Relayed agent sessions go through the exact same key verification +
    hello/registration path as direct /ws/agents connections; the broker never
    sees or checks agent keys. None when no broker is configured."""
    settings = deps.settings
    if not settings.broker_url:
        return None
    if not settings.broker_token:
        # load_settings enforces this for env-built settings; fail closed for
        # directly-constructed Settings too.
        raise RuntimeError(
            "broker_url is set but broker_token is empty; refusing to start"
        )
    if not settings.remote_exec_enabled:
        # Parity with /ws/agents, which refuses connections when remote
        # execution is off — don't accept them through the back door either.
        logger.warning(
            "AAKAAR_BROKER_URL is set but remote execution is disabled; "
            "broker link not started"
        )
        return None
    from aakaar.workers.remote.broker_link import BrokerLink

    return BrokerLink(
        url=settings.broker_url,
        token=settings.broker_token,
        session_factory=deps.session_factory,
        agent_registry=deps.agent_registry,
        recorder=deps.event_recorder,
        request_handler=deps.agent_request_handler,
        server_pubkey=(deps.agent_sealer.public_key_hex() if deps.agent_sealer is not None else None),
    )


def create_app(deps: AppDependencies) -> FastAPI:
    recordings = RecordingService(
        dispatcher=deps.remote_dispatcher, agents=deps.agent_registry
    )
    broker_link = _build_broker_link(deps)

    @asynccontextmanager
    async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
        logger.info("lifespan: startup")
        bootstrap_superuser(deps.settings, deps.session_factory)
        # Reconcile any runs left mid-flight by a previous (crashed/restarted)
        # process so the UI never shows perpetually-RUNNING zombies.
        try:
            deps.orchestrator.recover_interrupted_runs()
        except Exception:
            logger.exception("startup: interrupted-run recovery failed")
        # Replay run events a previous process persisted but never fanned out
        # (at-least-once outbox): subscribers that reconnect see the full
        # timeline; the UI dedupes on (run_id, sequence).
        try:
            deps.event_outbox.sweep()
        except Exception:
            logger.exception("startup: event outbox sweep failed")
        if deps.settings.scheduler_enabled:
            await deps.scheduler.start()
        # Escalate human tasks whose SLA deadline has passed even when no run
        # activity would otherwise drive the sweep.
        await deps.human_task_escalator.start()
        # Expires abandoned recording entries (and tells their agents to stop
        # capturing) even when no recordings request ever comes in again.
        await recordings.start()
        if broker_link is not None:
            # Dial OUT to the rendezvous broker; relayed agents register
            # through the same verification path as direct connections.
            await broker_link.start()
        try:
            yield
        finally:
            logger.info("lifespan: shutdown")
            if broker_link is not None:
                try:
                    await broker_link.stop()
                except Exception:
                    logger.exception("broker link shutdown failed")
            try:
                await deps.human_task_escalator.stop()
            except Exception:
                logger.exception("human-task escalator shutdown failed")
            try:
                await recordings.stop()
            except Exception:
                logger.exception("recording service shutdown failed")
            if deps.settings.scheduler_enabled:
                try:
                    await deps.scheduler.stop()
                except Exception:
                    logger.exception("scheduler shutdown failed")
            # Tear down any pool that exposes async `shutdown()` — currently
            # PlaywrightBrowserPool. FakeBrowserPool (used in tests) doesn't
            # need this; the duck-typed check keeps test wiring untouched.
            shutdown = getattr(deps.browser_pool, "shutdown", None)
            if shutdown is not None:
                try:
                    await shutdown()
                    logger.debug("browser pool shutdown complete")
                except Exception:
                    logger.exception("browser pool shutdown failed")

    app = FastAPI(title="Aakaar", version="0.1.0", lifespan=_lifespan)
    app.state.deps = deps
    app.state.recordings = recordings
    app.state.broker_link = broker_link  # None unless AAKAAR_BROKER_URL is set

    # Middleware note: Starlette runs the LAST-added middleware OUTERMOST. We
    # add CORS last so it wraps everything — even a 429 from the rate limiter
    # then carries CORS headers and the SPA can read the body. Inside CORS:
    # request-id (so every log line during the request is correlated), then the
    # rate limiter, then metrics, then the app/routers.
    if deps.settings.metrics_enabled:
        app.add_middleware(MetricsMiddleware)
    if deps.settings.rate_limit_enabled:
        app.add_middleware(
            RateLimitMiddleware,
            limiter=TokenBucketLimiter(
                default_per_min=deps.settings.rate_limit_per_min,
                auth_per_min=deps.settings.rate_limit_auth_per_min,
            ),
        )
    app.add_middleware(RequestIdMiddleware)

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
    app.include_router(mfa_router.router)
    app.include_router(oidc_router.router)
    app.include_router(jwks_router.router)
    app.include_router(superuser_router.router)
    app.include_router(admin_router.router)
    app.include_router(capabilities_router.router)
    app.include_router(workflows_router.router)
    app.include_router(runs_router.router)
    app.include_router(chat_router.router)
    app.include_router(chat_sessions_router.router)
    app.include_router(objects_router.router)
    app.include_router(stats_router.router)
    app.include_router(audit_router.router)
    app.include_router(approvals_router.router)
    app.include_router(retention_router.router)
    app.include_router(schedules_router.router)
    app.include_router(ws_router.router)
    app.include_router(agents_router.router)
    app.include_router(recordings_router.router)

    @app.get("/healthz")
    def _healthz() -> dict[str, str]:
        return {"status": "ok"}

    if deps.settings.metrics_enabled:

        @app.get("/metrics")
        def _metrics() -> object:
            return metrics_response()

    return app


__all__ = ["AppDependencies", "create_app"]
