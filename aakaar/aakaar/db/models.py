"""SQLAlchemy models for Aakaar.

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
from datetime import UTC, datetime
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
    return datetime.now(UTC)


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSON}


# ---------- enums (string-valued) ------------------------------------------


class TenantStatus:
    ACTIVE = "active"
    SUSPENDED = "suspended"


class UserRole:
    SUPERUSER = "superuser"  # Aakaar staff; tenant_id is None
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
    """The run stopped advancing. Payload `reason` distinguishes the cause:
    "human_prompt" (a human.prompt node is waiting) or "operator" (an
    explicit POST /runs/{id}/pause)."""
    RUN_RESUMED = "run_resumed"
    """Counterpart of RUN_PAUSED; same `reason` payload convention."""
    RUN_CANCELLED = "run_cancelled"
    """An operator cancel took effect and the run unwound to its terminal
    CANCELLED status. Payload: {"reason": "operator"}."""
    SIGNAL_RECEIVED = "signal_received"
    LIVE_SCREEN = "live_screen"
    """Best-effort screenshot of the active browser session captured after
    a node completes (success or failure). Payload: {"uri": str}. The UI
    renders the most recent one alongside the run timeline so an operator
    can see what the automation is looking at."""
    LOG = "log"


class RunMode:
    LIVE = "live"
    DRY_RUN = "dry_run"
    """A dry_run executes the DAG topology without performing money-moving or
    irreversible side effects — the simulation path. Default is LIVE."""


class WorkflowSensitivity:
    NORMAL = "normal"
    ELEVATED = "elevated"
    """ELEVATED marks money-moving / high-risk workflows. Combined with
    `requires_approval`, it gates publish/run behind maker-checker."""


class ApprovalSubjectType:
    WORKFLOW_PUBLISH = "workflow_publish"
    WORKFLOW_EDIT = "workflow_edit"
    RUN_START = "run_start"


class ApprovalStatus:
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


class HumanTaskStatus:
    PENDING = "pending"
    """Awaiting a human response; the SLA timer is live."""
    RESPONDED = "responded"
    EXPIRED = "expired"
    """The deadline passed with no response."""
    ESCALATED = "escalated"
    """Past `escalation_at` and reassigned/notified; still awaiting a response."""
    CANCELLED = "cancelled"
    """The run was cancelled or otherwise abandoned the prompt."""


class StoredObjectStatus:
    ACTIVE = "active"
    ERASED = "erased"
    """The underlying bytes were deleted by a right-to-erasure / retention
    sweep; the row is kept as a tombstone for audit."""


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
        # Unique when present (multiple NULLs allowed) so two concurrent
        # first-time OIDC logins can't provision duplicate users for the same
        # federated subject.
        Index("uq_users_oidc_subject", "oidc_subject", unique=True),
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

    # ---- MFA (TOTP) ----
    mfa_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    """True once an enrolled TOTP secret has been confirmed. While enrolling
    (secret generated, not yet confirmed) this stays False and the secret
    lives in `totp_pending_secret` so a half-finished enrollment can never
    lock the user out."""
    totp_secret: Mapped[str | None] = mapped_column(String(256), nullable=True)
    """Active TOTP secret (base32), optionally Fernet-encrypted at rest. NULL
    until enrollment is confirmed. Redacted from audit logs."""
    totp_pending_secret: Mapped[str | None] = mapped_column(String(256), nullable=True)
    """Candidate secret during enrollment, promoted to `totp_secret` on confirm."""
    totp_last_step: Mapped[int | None] = mapped_column(Integer, nullable=True)
    """Last TOTP time-step accepted, to reject replay of a code within its
    validity window."""
    mfa_recovery_codes: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    """`{"codes": [<bcrypt-hash>, ...]}` — single-use backup codes; each entry
    is removed as it is consumed. NULL when MFA is off."""

    # ---- OIDC / SSO ----
    oidc_subject: Mapped[str | None] = mapped_column(String(320), nullable=True)
    """Federated identity key, canonically `"{issuer}::{sub}"`. Unique when set
    (see uq_users_oidc_subject). NULL for password/local users."""
    last_login_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


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
    requires_approval: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    """When true, publishing or starting a run of this workflow must clear a
    maker-checker approval_request first (governance gate)."""
    sensitivity: Mapped[str] = mapped_column(
        String(32), nullable=False, default=WorkflowSensitivity.NORMAL
    )
    """'normal' | 'elevated'. Elevated flags money-moving workflows; the API
    layer may auto-set requires_approval for elevated ones."""
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    versions: Mapped[list[WorkflowVersion]] = relationship(
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
    requires_approval: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    """Per-version gate. Frozen at save time from the parent workflow so a
    later change to the workflow flag does not retroactively un-gate an
    already-approved version."""
    sensitivity: Mapped[str] = mapped_column(
        String(32), nullable=False, default=WorkflowSensitivity.NORMAL
    )
    """Per-version snapshot of the workflow sensitivity at save time."""
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
    mode: Mapped[str] = mapped_column(String(16), nullable=False, default=RunMode.LIVE)
    """'live' | 'dry_run'. A dry_run simulates the DAG without irreversible
    side effects. Set at run creation; never changes."""
    temporal_run_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    inputs: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    outputs: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    error: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    checkpoint: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    """Latest layer-boundary checkpoint for crash-safe resume, mirroring the
    newest `run_checkpoints` row for cheap single-read recovery:
    `{"layer_index": int, "completed_node_ids": [str, ...], "env": {node_id:
    {output_key: value}}}`. The interpreter writes it after settling each DAG
    layer; on restart, recovery loads it to resume from the next layer instead
    of failing the run. NULL until the first layer completes."""
    resume_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    """How many times this run has been resumed from a checkpoint after a
    restart. Bounds infinite resume loops on a poison run."""
    legal_hold: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    """When true, retention sweeps and right-to-erasure must skip this run
    (litigation / investigation hold)."""
    erased_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    """Set when a right-to-erasure / retention sweep has scrubbed this run's
    PII-bearing payloads (inputs/outputs/events). NULL while intact. The row
    itself is retained as an audit tombstone."""
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
        # Drives the at-least-once outbox sweep: find unpublished events fast,
        # in run+sequence order, after a restart.
        Index("ix_run_events_outbox", "published", "run_id", "sequence"),
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
    published: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    """Outbox flag for at-least-once fan-out to WS subscribers. The in-process
    publisher sets it true once the event has been dispatched; on restart the
    sweep replays every row still false so no subscriber misses an event."""
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    """When the event was marked published; NULL while pending."""
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
    __table_args__ = (
        Index("ix_audit_tenant_id", "tenant_id"),
        # Per-tenant monotonic ledger position, enforced as a UNIQUE INDEX
        # (not a table constraint) so it can be added by ALTER on SQLite too.
        # NULL seq rows (legacy / superuser/system entries with tenant_id NULL)
        # are unconstrained: multiple NULLs are distinct on SQLite and Postgres.
        # The chain is verified per tenant by ordering on `seq`.
        Index("uq_audit_tenant_seq", "tenant_id", "seq", unique=True),
    )

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
    seq: Mapped[int | None] = mapped_column(Integer, nullable=True)
    """Per-tenant monotonic sequence number (1, 2, 3, ...) assigned at write
    time. NULL on pre-chain / system (tenant_id NULL) rows. Stage 2 (audit
    service) assigns it under a per-tenant lock so the chain is gap-free."""
    prev_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    """Hex sha256 of the previous entry in this tenant's chain (its
    `entry_hash`). NULL for the genesis entry (seq=1) and pre-chain rows."""
    entry_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    """Hex sha256 over the canonicalized (prev_hash + this row's payload) —
    computed by stage 2. An exporter recomputes and compares to detect tamper.
    NULL on pre-chain rows."""
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class WorkflowSchedule(Base):
    """A scheduled trigger for a workflow.

    Exactly one of ``cron`` (recurring) or ``scheduled_at`` (one-off) is set —
    enforced in the API layer. The background scheduler polls this table,
    creates a Run for each due schedule, and stamps ``last_triggered_at``.
    """

    __tablename__ = "workflow_schedules"
    __table_args__ = (Index("ix_workflow_schedules_tenant_id", "tenant_id"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    workflow_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("workflows.id", ondelete="CASCADE"), nullable=False
    )
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    cron: Mapped[str | None] = mapped_column(String(128), nullable=True)
    scheduled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    inputs: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    executor_type: Mapped[str] = mapped_column(String(32), nullable=False, default="local")
    target: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )
    last_triggered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class RemoteAgent(Base):
    """A registered remote execution agent (workstation).

    Agents connect outbound to the server over an authenticated WebSocket and
    execute capability nodes whose DAG ``target`` selects them. ``(tenant_id,
    alias)`` is unique — agents are tenant-scoped (a run only ever dispatches to
    agents in its own tenant). Liveness (online/offline) is tracked in-memory by
    the registry; this row is the durable record + last-known metadata.
    """

    __tablename__ = "remote_agents"
    __table_args__ = (
        UniqueConstraint("tenant_id", "alias", name="uq_remote_agent_tenant_alias"),
        Index("ix_remote_agents_tenant_id", "tenant_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    alias: Mapped[str] = mapped_column(String(64), nullable=False)
    api_key_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    os: Mapped[str | None] = mapped_column(String(32), nullable=True)
    hostname: Mapped[str | None] = mapped_column(String(255), nullable=True)
    gui_capable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    pools: Mapped[list[Any]] = mapped_column(JSON, default=list)
    capabilities: Mapped[list[Any]] = mapped_column(JSON, default=list)
    agent_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="enrolled")
    last_seen: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class RemoteAgentStatus:
    ENROLLED = "enrolled"
    ONLINE = "online"
    OFFLINE = "offline"


class RunCheckpoint(Base):
    """Per-layer durable checkpoint for crash-safe run resume.

    The in-process interpreter walks the DAG in topological layers and, after
    settling each layer, writes one row here capturing the completed node ids
    and the full output env snapshot up to that boundary. On restart, recovery
    loads the highest `layer_index` for a non-terminal run and resumes from the
    next layer instead of failing the run.

    `runs.checkpoint` mirrors the newest row for a single-read fast path; this
    table keeps the per-layer history (useful for rerun-from-layer and audit).
    `(run_id, layer_index)` is unique so a re-driven layer overwrites rather
    than duplicates.
    """

    __tablename__ = "run_checkpoints"
    __table_args__ = (
        UniqueConstraint("run_id", "layer_index", name="uq_run_checkpoint_layer"),
        Index("ix_run_checkpoints_tenant_id", "tenant_id"),
        Index("ix_run_checkpoints_run_id", "run_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("runs.id", ondelete="CASCADE"), nullable=False
    )
    layer_index: Mapped[int] = mapped_column(Integer, nullable=False)
    """0-based topological layer this checkpoint completes. Resume starts at
    `layer_index + 1`."""
    completed_node_ids: Mapped[list[Any]] = mapped_column(JSON, default=list)
    """Node ids whose outputs are reflected in `env` (everything through this
    layer). Lets resume skip already-done nodes."""
    env: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    """Output environment snapshot `{node_id: {output_key: value}}` at this
    layer boundary — the executor seeds its env from this on resume. Redacted
    of secrets by the writer, same convention as `runs.outputs`."""
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class ApprovalRequest(Base):
    """A maker-checker approval gate.

    A 'maker' raises a request to publish a workflow, edit a sensitive
    workflow, or start a run; a 'checker' (different user, enforced in stage 2)
    approves or rejects it. The gated action proceeds only on `approved`.
    """

    __tablename__ = "approval_requests"
    __table_args__ = (
        Index("ix_approval_requests_tenant_id", "tenant_id"),
        Index("ix_approval_requests_tenant_status", "tenant_id", "status"),
        Index("ix_approval_requests_subject", "tenant_id", "subject_type", "subject_ref"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    subject_type: Mapped[str] = mapped_column(String(32), nullable=False)
    """'workflow_publish' | 'workflow_edit' | 'run_start' | other (see
    ApprovalSubjectType)."""
    subject_ref: Mapped[str] = mapped_column(String(128), nullable=False)
    """The id of the gated resource (workflow id, version id, or a pending-run
    correlation id), stored as a string so any subject kind fits one column."""
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=ApprovalStatus.PENDING
    )
    """'pending' | 'approved' | 'rejected' | 'cancelled'."""
    requested_by: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )
    decided_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    decided_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    reason: Mapped[str] = mapped_column(String(2000), default="")
    """Free-text decision note (why approved/rejected)."""
    context: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    """Snapshot the checker needs to decide without chasing other tables:
    e.g. workflow name, version, sensitivity, run inputs summary, a diff."""


class RetentionPolicy(Base):
    """A per-tenant, per-resource-type retention rule.

    Stage 2's retention sweep reads these to decide what to erase/expire.
    `(tenant_id, resource_type)` is unique — one policy per resource kind.
    A NULL `ttl_days` means 'retain indefinitely' (no automatic expiry).
    """

    __tablename__ = "retention_policies"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "resource_type", name="uq_retention_tenant_resource"
        ),
        Index("ix_retention_policies_tenant_id", "tenant_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    resource_type: Mapped[str] = mapped_column(String(64), nullable=False)
    """Logical resource the policy governs, e.g. 'run', 'run_event',
    'stored_object', 'audit_log', 'chat_session'."""
    ttl_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    """Days to retain after the resource's reference timestamp; NULL = keep
    forever."""
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class StoredObject(Base):
    """Durable metadata for an object held in the filesystem object store.

    The object_store driver writes bytes under aakaar://t/{tenant}/{key}; this
    row is the DB-side record so retention sweeps, legal hold, and
    right-to-erasure have something queryable to act on (the store itself has
    no index). Stage 2 records a row on every managed write and flips
    `status`/`erased_at` when the bytes are scrubbed.
    """

    __tablename__ = "stored_objects"
    __table_args__ = (
        UniqueConstraint("tenant_id", "uri", name="uq_stored_object_uri"),
        Index("ix_stored_objects_tenant_id", "tenant_id"),
        Index("ix_stored_objects_run_id", "run_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("runs.id", ondelete="SET NULL"), nullable=True
    )
    """The run that produced the object, if any (artifacts, downloads,
    screenshots). NULL for non-run uploads."""
    uri: Mapped[str] = mapped_column(String(512), nullable=False)
    """Canonical aakaar:// URI returned by the object store."""
    key: Mapped[str] = mapped_column(String(512), nullable=False)
    """Tenant-relative key (the part after the scheme/tenant prefix)."""
    kind: Mapped[str | None] = mapped_column(String(64), nullable=True)
    """Logical artifact kind, e.g. 'download', 'screenshot', 'report'."""
    size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=StoredObjectStatus.ACTIVE
    )
    """'active' | 'erased'. 'erased' is a tombstone: the bytes are gone but the
    audit row remains."""
    legal_hold: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    """When true, retention/erasure must skip this object."""
    erased_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    """When the bytes were scrubbed; NULL while intact."""
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class HumanTask(Base):
    """A persisted, SLA-bounded human-in-the-loop prompt.

    The in-process SignalHub holds a pending `human.prompt` only in memory; this
    row makes it durable so a deadline/escalation timer survives a restart and
    an operator can see outstanding tasks. Stage 2 writes a row when a prompt
    opens, resolves it on response, and a background timer flips status to
    'expired'/'escalated' at the deadlines.

    `(run_id, node_id)` is unique — at most one live prompt per node, matching
    the SignalHub's in-memory invariant.
    """

    __tablename__ = "human_tasks"
    __table_args__ = (
        UniqueConstraint("run_id", "node_id", name="uq_human_task_run_node"),
        Index("ix_human_tasks_tenant_id", "tenant_id"),
        Index("ix_human_tasks_tenant_status", "tenant_id", "status"),
        # SLA timer sweep: find live tasks whose deadline/escalation has passed.
        Index("ix_human_tasks_deadline", "status", "deadline_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("runs.id", ondelete="CASCADE"), nullable=False
    )
    node_id: Mapped[str] = mapped_column(String(64), nullable=False)
    prompt: Mapped[str] = mapped_column(String(8000), default="")
    """The message shown to the human (the SignalHub `message`)."""
    expects: Mapped[str] = mapped_column(String(16), nullable=False, default="text")
    """'text' | 'otp' | 'confirm' — mirrors SignalExpects; shapes the input."""
    assigned_role: Mapped[str | None] = mapped_column(String(32), nullable=True)
    """Role expected to respond (e.g. 'tenant_admin'); NULL = anyone in tenant."""
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=HumanTaskStatus.PENDING
    )
    """'pending' | 'responded' | 'expired' | 'escalated' | 'cancelled'."""
    deadline_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    """SLA deadline; past it with no response the sweep marks 'expired'.
    NULL = no SLA."""
    escalation_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    """When to escalate (notify/reassign) if still pending; NULL = none."""
    escalation: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    """Escalation metadata, e.g. `{"notify_role": "...", "reassign_to": "..."}`."""
    responded_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    responded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    response: Mapped[str | None] = mapped_column(String(8000), nullable=True)
    """The human's answer (resolves the SignalHub future). Redact OTPs in the
    writer before persisting."""
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
