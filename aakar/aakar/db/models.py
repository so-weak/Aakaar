"""SQLAlchemy models for Aakar.

Schema goals:
  - Portable across SQLite (dev) and Yugabyte/Postgres (prod). No dialect-only
    types in v1. UUIDs use the SQLAlchemy `Uuid` type which adapts cleanly.
  - Every domain table carries `tenant_id` as the first non-id column. This is
    enforced by the API layer; on Postgres/Yugabyte we'll layer RLS on top in
    a follow-up migration.
  - Workflow DAGs are stored as JSON. We do not normalize nodes/edges into
    separate tables — the validated DAG is the unit of save / version / run.

Status enums are stored as plain strings to keep migrations simple across
dialects. Constants live alongside the models.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSON}


# ---------- enums (string-valued) ------------------------------------------


class TenantStatus:
    ACTIVE = "active"
    SUSPENDED = "suspended"


class UserRole:
    SUPERUSER = "superuser"  # Aakar staff; tenant_id is None
    TENANT_ADMIN = "tenant_admin"
    TENANT_USER = "tenant_user"


class UserStatus:
    ACTIVE = "active"
    DISABLED = "disabled"


class RunStatus:
    QUEUED = "queued"
    RUNNING = "running"
    PAUSED = "paused"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class RunEventKind:
    NODE_STARTED = "node_started"
    NODE_COMPLETED = "node_completed"
    NODE_FAILED = "node_failed"
    NODE_RETRYING = "node_retrying"
    RUN_PAUSED = "run_paused"
    RUN_RESUMED = "run_resumed"
    SIGNAL_RECEIVED = "signal_received"
    LOG = "log"


# ---------- tables ---------------------------------------------------------


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    slug: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default=TenantStatus.ACTIVE)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("tenant_id", "email", name="uq_users_tenant_email"),
        Index("ix_users_tenant_id", "tenant_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # Superuser rows have tenant_id = NULL; all other users belong to a tenant.
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=True
    )
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default=UserStatus.ACTIVE)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class CapabilityGrant(Base):
    """A tenant's permission to use a capability under a specific account alias.

    The capability *definition* lives in code (staff-authored). The *grant*
    binds (capability_ref, account_alias) to a vault path that holds the
    actual credentials. The DAG references account_alias only.
    """

    __tablename__ = "capability_grants"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "capability_ref", "account_alias", name="uq_grants_alias"
        ),
        Index("ix_grants_tenant_id", "tenant_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    capability_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    account_alias: Mapped[str] = mapped_column(String(64), nullable=False)
    vault_ref: Mapped[str] = mapped_column(String(512), nullable=False)
    input_defaults: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    created_by: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )


class Workflow(Base):
    __tablename__ = "workflows"
    __table_args__ = (Index("ix_workflows_tenant_id", "tenant_id"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    created_by: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(String(2000), default="")
    latest_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    versions: Mapped[list["WorkflowVersion"]] = relationship(
        back_populates="workflow", cascade="all, delete-orphan"
    )


class WorkflowVersion(Base):
    __tablename__ = "workflow_versions"
    __table_args__ = (
        UniqueConstraint("workflow_id", "version", name="uq_workflow_version"),
        Index("ix_workflow_versions_tenant_id", "tenant_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    workflow_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("workflows.id", ondelete="CASCADE"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    dag: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    rationale: Mapped[str] = mapped_column(String(4000), default="")
    created_by: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    workflow: Mapped[Workflow] = relationship(back_populates="versions")


class Run(Base):
    __tablename__ = "runs"
    __table_args__ = (Index("ix_runs_tenant_id", "tenant_id"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    workflow_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("workflows.id", ondelete="CASCADE"), nullable=False
    )
    workflow_version: Mapped[int] = mapped_column(Integer, nullable=False)
    started_by: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, default=RunStatus.QUEUED)
    temporal_run_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    inputs: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    outputs: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    error: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class RunEvent(Base):
    """Denormalized timeline for the run-detail UI.

    Temporal is the source of truth for what happened during a run; this table
    is a redacted, UI-friendly mirror written by the interpreter as events
    occur. Keep payloads small and never include secrets.
    """

    __tablename__ = "run_events"
    __table_args__ = (
        UniqueConstraint("run_id", "sequence", name="uq_run_event_seq"),
        Index("ix_run_events_tenant_id", "tenant_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("runs.id", ondelete="CASCADE"), nullable=False
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    node_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class ChatSession(Base):
    """A conversational planning session.

    Each session owns an ordered list of `ChatMessage` rows and a draft DAG.
    `workflow_id` is set once the session has been saved to a workflow;
    while it is null the session is unbound and saving creates a new
    workflow. After save, the session can keep going — subsequent planner
    turns may produce a `draft_dag` that diverges from the persisted
    workflow, and the UI surfaces an "update saved workflow" affordance.
    """

    __tablename__ = "chat_sessions"
    __table_args__ = (Index("ix_chat_sessions_tenant_id", "tenant_id"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(255), default="Untitled session")
    workflow_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("workflows.id", ondelete="SET NULL"), nullable=True
    )
    saved_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    """Workflow version most recently saved from this session. Used for
    drift detection — `draft_dag` is dirty iff it differs from the version
    `saved_version` of `workflow_id`."""
    draft_dag: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    """Latest DAG the planner produced this session, or None if no DAG yet."""
    draft_rationale: Mapped[str] = mapped_column(String(2000), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class ChatMessage(Base):
    """One turn in a chat session — either user input or planner reply."""

    __tablename__ = "chat_messages"
    __table_args__ = (
        Index("ix_chat_messages_session_id", "session_id"),
        UniqueConstraint("session_id", "sequence", name="uq_chat_messages_session_sequence"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    session_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("chat_sessions.id", ondelete="CASCADE"), nullable=False
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    """'user' | 'planner'."""
    text: Mapped[str] = mapped_column(String(8000), default="")
    """For role=user: the message they typed.
    For role=planner: the rationale/explanation surfaced in the chat bubble
    (the full structured response lives in `payload`)."""
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    """For role=planner: the full PlannerCompletion dict (kind, dag,
    questions, needed, explanation, rationale). Empty for role=user."""
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class AuditLog(Base):
    __tablename__ = "audit_log"
    __table_args__ = (Index("ix_audit_tenant_id", "tenant_id"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=True
    )
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    target_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    target_id: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
