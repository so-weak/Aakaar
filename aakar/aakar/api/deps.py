"""FastAPI dependencies and the shared `AppDependencies` bundle.

`AppDependencies` is the singleton registry of long-lived components (DB
engine, LLM client, vault, registry, etc.) attached to the FastAPI app at
startup. Request-time dependencies pull from it.

Tests construct a fresh `AppDependencies` per app instance and can also
swap individual factories via `app.dependency_overrides`.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from aakar.api.auth import InvalidToken, TokenClaims, verify_token
from aakar.api.config import Settings
from aakar.capabilities import load_into as load_capabilities
from aakar.db.models import User, UserRole, UserStatus
from aakar.db.session import SessionFactory
from aakar.db.tenancy import tenant_scope
from aakar.interpreter import (
    ActivityRegistry,
    EventRecorder,
    Executor,
    LocalExecutor,
    RunOrchestrator,
    build_default_activities,
)
from aakar.interpreter.events import DbEventRecorder
from aakar.interpreter.signals import SignalHub
from aakar.planner import (
    CapabilityIndex,
    EmbeddingsClient,
    LLMClient,
    PlannerService,
)
from aakar.shared.registry import Registry
from aakar.storage.object_store import LocalFsObjectStore, ObjectStorage
from aakar.storage.vector_store import VectorStore
from aakar.vault import Vault
from aakar.workers.browser import BrowserPool


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
    """If True (default), the `aakar.capabilities` package is walked at startup
    and every capability module is registered into the registry + activities."""
    capability_index: CapabilityIndex = field(init=False)
    planner: PlannerService = field(init=False)
    signals: SignalHub = field(init=False)
    executor: Executor = field(init=False)
    orchestrator: RunOrchestrator = field(init=False)

    def __post_init__(self) -> None:
        self.capability_index = CapabilityIndex(
            registry=self.registry,
            embeddings=self.embeddings,
            vector_store=self.vector_store,
        )
        self.planner = PlannerService(registry=self.registry, llm=self.llm)
        if self.activities is None:
            self.activities = build_default_activities()
        if self.autoload_capabilities:
            load_capabilities(self.registry, self.activities)
        if self.event_recorder is None:
            self.event_recorder = DbEventRecorder(session_factory=self.session_factory)
        self.signals = SignalHub()
        self.executor = LocalExecutor(
            activities=self.activities,
            recorder=self.event_recorder,
            signals=self.signals,
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
        )


# ---------- generic accessors ---------------------------------------------


def get_deps(request: Request) -> AppDependencies:
    deps = getattr(request.app.state, "deps", None)
    if deps is None:
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


# ---------- auth ----------------------------------------------------------


_bearer = HTTPBearer(auto_error=False, description="Aakar JWT access token")


def get_current_claims(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> TokenClaims:
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        return verify_token(
            credentials.credentials,
            secret=settings.jwt_secret,
            algorithm=settings.jwt_algorithm,
        )
    except InvalidToken as e:
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
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="user not active")
    if user.role != claims.role:
        # Role drifted since the token was issued — invalidate.
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="role changed")
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
