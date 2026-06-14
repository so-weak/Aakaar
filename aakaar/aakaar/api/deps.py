"""FastAPI dependencies and the shared `AppDependencies` bundle.

`AppDependencies` is the singleton registry of long-lived components (DB
engine, LLM client, vault, registry, etc.) attached to the FastAPI app at
startup. Request-time dependencies pull from it.

Tests construct a fresh `AppDependencies` per app instance and can also
swap individual factories via `app.dependency_overrides`.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from aakaar.api.auth import (
    InvalidToken,
    KeyStore,
    TokenClaims,
    is_asymmetric,
    verify_access_token,
)
from aakaar.api.auth.oidc import OidcClient
from aakaar.capabilities import load_into as load_capabilities
from aakaar.core.config import Settings
from aakaar.db.models import User, UserRole, UserStatus
from aakaar.db.session import SessionFactory
from aakaar.db.tenancy import tenant_scope
from aakaar.interpreter import (
    ActivityRegistry,
    EventRecorder,
    Executor,
    LocalExecutor,
    RunOrchestrator,
    build_default_activities,
)
from aakaar.interpreter.events import DbEventRecorder
from aakaar.interpreter.signals import SignalHub
from aakaar.planner import (
    CapabilityIndex,
    EmbeddingsClient,
    LLMClient,
    PlannerService,
)
from aakaar.services.audit import AuditFileSink, AuditRecorder
from aakaar.services.events import BroadcastingEventRecorder, EventBroker
from aakaar.services.scheduler import Scheduler
from aakaar.shared.registry import Registry
from aakaar.storage.object_store import LocalFsObjectStore, ObjectStorage
from aakaar.storage.vector_store import VectorStore
from aakaar.vault import Vault
from aakaar.workers.browser import BrowserPool
from aakaar.workers.remote import AgentRegistry, RemoteDispatcher

if TYPE_CHECKING:
    # Imported lazily at runtime inside __post_init__ (only when a browser_pool
    # is configured); referenced here solely for the field annotation.
    from aakaar.planner.agentic.service import AgenticPlannerService

logger = logging.getLogger(__name__)


# ---------- AppDependencies ------------------------------------------------


@dataclass
class AppDependencies:
    settings: Settings
    engine: Engine
    session_factory: SessionFactory
    registry: Registry
    llm: LLMClient
    embeddings: EmbeddingsClient
    vector_store: VectorStore
    object_store: ObjectStorage
    vault: Vault
    activities: ActivityRegistry | None = None
    event_recorder: EventRecorder | None = None
    browser_pool: BrowserPool | None = None
    """Optional. Tests pass FakeBrowserPool; production passes PlaywrightBrowserPool.
    None means browser activities will fail at execution with a clear error."""
    autoload_capabilities: bool = True
    """If True (default), the `aakaar.capabilities` package is walked at startup
    and every capability module is registered into the registry + activities."""
    capability_index: CapabilityIndex = field(init=False)
    planner: PlannerService = field(init=False)
    agentic_planner: AgenticPlannerService | None = field(init=False, default=None)
    """Tool-driven fallback planner. None when no browser_pool is configured —
    the agentic loop needs a Playwright session and there's nothing to drive
    in headless-only deployments."""
    signals: SignalHub = field(init=False)
    executor: Executor = field(init=False)
    orchestrator: RunOrchestrator = field(init=False)
    audit: AuditRecorder = field(init=False)
    scheduler: Scheduler = field(init=False)
    event_broker: EventBroker = field(init=False)
    agent_registry: AgentRegistry = field(init=False)
    remote_dispatcher: RemoteDispatcher | None = field(init=False, default=None)
    key_store: KeyStore | None = field(init=False, default=None)
    """RSA signing keys; populated only when jwt_algorithm is asymmetric (RS*).
    None under HS256 (the symmetric default)."""
    oidc: OidcClient = field(init=False)

    def __post_init__(self) -> None:
        logger.debug("AppDependencies: wiring derived components")
        if is_asymmetric(self.settings.jwt_algorithm):
            if self.settings.jwt_key_dir is None:
                raise RuntimeError(
                    f"jwt_algorithm={self.settings.jwt_algorithm} requires a key "
                    "directory; set AAKAAR_JWT_KEY_DIR (and AAKAAR_JWT_BOOTSTRAP_KEYS=true "
                    "for dev key generation)."
                )
            self.key_store = KeyStore.from_dir(
                self.settings.jwt_key_dir,
                algorithm=self.settings.jwt_algorithm,
                bootstrap=self.settings.jwt_bootstrap_keys,
            )
        self.oidc = OidcClient(self.settings)
        self.audit = AuditRecorder(
            session_factory=self.session_factory,
            sink=AuditFileSink(self.settings.data_dir),
        )
        self.capability_index = CapabilityIndex(
            registry=self.registry,
            embeddings=self.embeddings,
            vector_store=self.vector_store,
        )
        self.planner = PlannerService(registry=self.registry, llm=self.llm)
        if self.browser_pool is not None:
            from aakaar.planner.agentic.service import AgenticPlannerService

            self.agentic_planner = AgenticPlannerService(
                registry=self.registry,
                llm=self.llm,
                browser_pool=self.browser_pool,
                vault=self.vault,
            )
            logger.debug("agentic planner wired")
        else:
            logger.info("agentic planner disabled (no browser_pool)")
        if self.activities is None:
            self.activities = build_default_activities()
        if self.autoload_capabilities:
            logger.debug("autoloading capabilities into registry/activities")
            load_capabilities(self.registry, self.activities)
        self.event_broker = EventBroker()
        if self.event_recorder is None:
            # Wrap the canonical DB recorder so each event also fans out to any
            # live WebSocket subscribers. Test-provided recorders are left as-is.
            self.event_recorder = BroadcastingEventRecorder(
                DbEventRecorder(session_factory=self.session_factory),
                self.event_broker,
            )
        self.signals = SignalHub()
        self.agent_registry = AgentRegistry()
        if self.settings.remote_exec_enabled:
            self.remote_dispatcher = RemoteDispatcher(
                agents=self.agent_registry,
                registry=self.registry,
                audit=self.audit,
                recorder=self.event_recorder,
                default_timeout_s=self.settings.remote_task_timeout_seconds,
            )
        self.executor = LocalExecutor(
            activities=self.activities,
            recorder=self.event_recorder,
            signals=self.signals,
            llm=self.llm,
            live_screenshots=self.settings.live_screenshots,
            remote_dispatcher=self.remote_dispatcher,
        )
        self.orchestrator = RunOrchestrator(
            session_factory=self.session_factory,
            executor=self.executor,
            signals=self.signals,
            recorder=self.event_recorder,
            registry=self.registry,
            object_store=self.object_store,
            vault=self.vault,
            browser_pool=self.browser_pool,
            download_mirror_dir=self.settings.download_mirror_dir,
        )
        self.scheduler = Scheduler(
            session_factory=self.session_factory,
            orchestrator=self.orchestrator,
            tick_seconds=self.settings.scheduler_tick_seconds,
        )
        logger.debug("AppDependencies: ready")


# ---------- generic accessors ---------------------------------------------


def get_deps(request: Request) -> AppDependencies:
    deps = getattr(request.app.state, "deps", None)
    if not isinstance(deps, AppDependencies):
        raise RuntimeError("AppDependencies not attached to app.state.deps")
    return deps


def get_settings(deps: Annotated[AppDependencies, Depends(get_deps)]) -> Settings:
    return deps.settings


def get_session(
    deps: Annotated[AppDependencies, Depends(get_deps)],
) -> Iterator[Session]:
    s = deps.session_factory.session()
    try:
        yield s
    finally:
        s.close()


def get_registry(deps: Annotated[AppDependencies, Depends(get_deps)]) -> Registry:
    return deps.registry


def get_planner(deps: Annotated[AppDependencies, Depends(get_deps)]) -> PlannerService:
    return deps.planner


def get_agentic_planner(
    deps: Annotated[AppDependencies, Depends(get_deps)],
) -> AgenticPlannerService | None:
    """Returns the agentic planner if configured, else None. Routers branch
    on the Optional rather than receiving a NotImplemented stub."""
    return deps.agentic_planner


def get_vault(deps: Annotated[AppDependencies, Depends(get_deps)]) -> Vault:
    return deps.vault


def get_capability_index(
    deps: Annotated[AppDependencies, Depends(get_deps)],
) -> CapabilityIndex:
    return deps.capability_index


def get_object_store(
    deps: Annotated[AppDependencies, Depends(get_deps)],
) -> ObjectStorage:
    return deps.object_store


def get_orchestrator(
    deps: Annotated[AppDependencies, Depends(get_deps)],
) -> RunOrchestrator:
    return deps.orchestrator


def get_audit(deps: Annotated[AppDependencies, Depends(get_deps)]) -> AuditRecorder:
    return deps.audit


def get_agent_registry(
    deps: Annotated[AppDependencies, Depends(get_deps)],
) -> AgentRegistry:
    return deps.agent_registry


# ---------- auth ----------------------------------------------------------


def get_key_store(
    deps: Annotated[AppDependencies, Depends(get_deps)],
) -> KeyStore | None:
    return deps.key_store


def get_oidc(deps: Annotated[AppDependencies, Depends(get_deps)]) -> OidcClient:
    return deps.oidc


_bearer = HTTPBearer(auto_error=False, description="Aakaar JWT access token")


def get_current_claims(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    deps: Annotated[AppDependencies, Depends(get_deps)],
) -> TokenClaims:
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        return verify_access_token(credentials.credentials, deps.settings, deps.key_store)
    except InvalidToken as e:
        logger.info("auth: rejected token (%s)", e)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(e),
            headers={"WWW-Authenticate": "Bearer"},
        ) from e


def get_current_user(
    claims: Annotated[TokenClaims, Depends(get_current_claims)],
    session: Annotated[Session, Depends(get_session)],
) -> User:
    user = session.get(User, claims.user_id)
    if user is None or user.status != UserStatus.ACTIVE:
        logger.info("auth: user_id=%s not active", claims.user_id)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="user not active")
    if user.role != claims.role:
        # Role drifted since the token was issued — invalidate.
        logger.info(
            "auth: role drift user_id=%s token_role=%s db_role=%s",
            user.id,
            claims.role,
            user.role,
        )
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="role changed")
    # Enforce MFA: a user who has enabled TOTP must present a token whose `amr`
    # proves a second factor (totp at login, or an OIDC login carrying one).
    # This makes MFA actually binding — a pre-enrollment ("pwd"-only) token
    # stops working the moment MFA is turned on, forcing a fresh login.
    if user.mfa_enabled and not ({"totp", "oidc"} & set(claims.amr)):
        logger.info("auth: user_id=%s has MFA enabled but token lacks 2FA amr", user.id)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="mfa required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    # Defence-in-depth for tenant suspension: even if the suspend cascade
    # missed flipping a row (or a new user was created against a
    # suspended tenant somehow), refuse the request. Superusers
    # (tenant_id is None) are exempt — they need to be able to log in
    # to fix a suspended tenant.
    if user.tenant_id is not None:
        from aakaar.db.models import Tenant, TenantStatus

        tenant = session.get(Tenant, user.tenant_id)
        if tenant is not None and tenant.status == TenantStatus.SUSPENDED:
            logger.warning(
                "auth: blocked user_id=%s on suspended tenant_id=%s",
                user.id,
                user.tenant_id,
            )
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="tenant suspended",
            )
    return user


def require_superuser(user: Annotated[User, Depends(get_current_user)]) -> User:
    if user.role != UserRole.SUPERUSER:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="superuser only")
    return user


def require_tenant_admin(user: Annotated[User, Depends(get_current_user)]) -> User:
    if user.role not in (UserRole.TENANT_ADMIN,):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="tenant admin only")
    if user.tenant_id is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="user has no tenant")
    return user


def require_tenant_user(user: Annotated[User, Depends(get_current_user)]) -> User:
    if user.role not in (UserRole.TENANT_ADMIN, UserRole.TENANT_USER):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="tenant user only")
    if user.tenant_id is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="user has no tenant")
    return user


def require_mfa_satisfied(
    user: Annotated[User, Depends(get_current_user)],
    claims: Annotated[TokenClaims, Depends(get_current_claims)],
) -> User:
    """Step-up guard for especially sensitive routes: require a second factor
    in the token regardless of whether the user has MFA toggled on. Apply with
    `Depends(require_mfa_satisfied)` where you want to force 2FA."""
    if not ({"totp", "oidc"} & set(claims.amr)):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="second-factor authentication required",
        )
    return user


# ---------- tenant scope helper -------------------------------------------


@contextmanager
def tenant_scope_for(user: User) -> Iterator[uuid.UUID]:
    """Convenience: enter a tenant_scope for a tenant-bound user. Routers use
    this inside endpoints that touch tenant-scoped tables."""
    if user.tenant_id is None:
        raise HTTPException(status_code=400, detail="user has no tenant context")
    with tenant_scope(user.tenant_id):
        yield user.tenant_id


# ---------- helper used by app.py for default wiring ----------------------


def default_object_store(data_dir: Path) -> LocalFsObjectStore:
    return LocalFsObjectStore(data_dir)


def find_user_by_email(
    session: Session, *, tenant_id: uuid.UUID | None, email: str
) -> User | None:
    stmt = select(User).where(User.email == email)
    stmt = stmt.where(User.tenant_id == tenant_id) if tenant_id is not None else stmt.where(
        User.tenant_id.is_(None)
    )
    return session.scalars(stmt).first()
