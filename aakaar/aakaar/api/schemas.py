"""HTTP request/response shapes.

Separate from DB models on purpose — they evolve at different cadences and
serve different audiences. Keep these dialect-agnostic and avoid leaking
internal-only fields (e.g. password_hash, vault_ref values).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from aakaar.shared.dag.types import Dag

# Permissive email regex — RFC-strict validators reject reserved TLDs (.test,
# .example) and other valid-but-uncommon shapes. We accept anything that looks
# like local@domain.tld; admins are expected to enter real addresses.
EmailStr = Annotated[
    str,
    Field(
        min_length=3,
        max_length=320,
        pattern=r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$",
    ),
]


# ---------- auth ----------------------------------------------------------


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: EmailStr
    password: str


class LoginResponse(BaseModel):
    access_token: str | None = None
    """The bearer access token. None when `mfa_required` — finish the step-up
    at /auth/mfa/verify to obtain it."""
    token_type: str = "Bearer"
    expires_at: datetime | None = None
    tenant_slug: str | None = None
    """Tenant slug if the user belongs to one. The frontend uses this to
    render role labels like 'PayOps user' instead of 'tenant user'."""
    tenant_name: str | None = None
    """Tenant display name if the user belongs to one. Same purpose as
    `tenant_slug` but surfaced where a longer label is preferred."""
    mfa_required: bool = False
    """True when the password was correct but a second factor is needed."""
    mfa_token: str | None = None
    """Short-lived MFA step-up ticket (present iff `mfa_required`). POST it with
    a TOTP code (or recovery code) to /auth/mfa/verify for the access token."""


# ---------- MFA (TOTP) ----------------------------------------------------

_CODE = Field(min_length=6, max_length=10, pattern=r"^[0-9]+$")


class MfaEnrollResponse(BaseModel):
    secret: str
    """Base32 TOTP secret — for manual entry into an authenticator app."""
    otpauth_url: str
    """`otpauth://` provisioning URI; the SPA renders it as a QR code."""


class MfaConfirmRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str = _CODE


class MfaConfirmResponse(BaseModel):
    recovery_codes: list[str]
    """One-time backup codes — shown ONCE at enrollment; store them safely."""


class MfaDisableRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str = _CODE


class MfaStatusResponse(BaseModel):
    enabled: bool
    pending: bool
    """True when a secret is enrolled but not yet confirmed."""


class MfaVerifyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mfa_token: str
    code: str | None = Field(default=None, min_length=6, max_length=10, pattern=r"^[0-9]+$")
    recovery_code: str | None = None


# ---------- tenants -------------------------------------------------------


class TenantCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    slug: str = Field(min_length=2, max_length=64, pattern=r"^[a-z][a-z0-9-]*$")
    name: str = Field(min_length=1, max_length=255)
    admin_email: EmailStr
    admin_password: str = Field(min_length=8, max_length=128)


class TenantResponse(BaseModel):
    id: uuid.UUID
    slug: str
    name: str
    status: str
    created_at: datetime


# ---------- users ---------------------------------------------------------


class UserCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    role: str = Field(pattern=r"^(tenant_admin|tenant_user)$")


class UserUpdateRequest(BaseModel):
    """Edit an existing user. All fields optional; supply only what changes.

    Email is intentionally immutable — mutating it would orphan run history
    and audit references; create a new user instead.
    """

    model_config = ConfigDict(extra="forbid")
    role: str | None = Field(
        default=None, pattern=r"^(tenant_admin|tenant_user)$"
    )
    password: str | None = Field(default=None, min_length=8, max_length=128)


class UserResponse(BaseModel):
    id: uuid.UUID
    tenant_id: uuid.UUID | None
    email: EmailStr
    role: str
    status: str
    created_at: datetime


# ---------- capability grants --------------------------------------------


class GrantCreateRequest(BaseModel):
    """Tenant admin creates a grant by providing the capability ref, an
    account alias, and the secret values. Secret values are stored in the
    vault and are not returned by any endpoint."""

    model_config = ConfigDict(extra="forbid")
    capability_ref: str = Field(pattern=r"^cap\.[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*$")
    account_alias: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_-]*$")
    secrets: dict[str, str] = Field(default_factory=dict)
    input_defaults: dict[str, Any] = Field(default_factory=dict)


class GrantUpdateRequest(BaseModel):
    """Patch an existing grant. All fields optional — supply only what
    changes. `secrets`, when supplied, must contain *every* declared
    secret name for the capability (we don't allow partial rotation;
    that's a footgun)."""

    model_config = ConfigDict(extra="forbid")
    account_alias: str | None = Field(
        default=None,
        min_length=1,
        max_length=64,
        pattern=r"^[a-z][a-z0-9_-]*$",
    )
    secrets: dict[str, str] | None = None
    input_defaults: dict[str, Any] | None = None
    enabled: bool | None = None


class GrantResponse(BaseModel):
    id: uuid.UUID
    capability_ref: str
    account_alias: str
    secret_names: list[str]  # names only; values are vault-only
    input_defaults: dict[str, Any]
    enabled: bool
    created_at: datetime


# ---------- capabilities --------------------------------------------------


class CapabilityFieldInfo(BaseModel):
    name: str
    type_label: str
    required: bool
    description: str = ""


class CapabilityDefinitionResponse(BaseModel):
    ref: str
    kind: str
    description: str
    inputs: list[CapabilityFieldInfo]
    outputs: list[CapabilityFieldInfo]
    secret_names: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)


# ---------- workflows -----------------------------------------------------


class WorkflowCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=255)
    description: str = ""
    dag: Dag
    rationale: str = ""
    requires_approval: bool = False
    """Opt the workflow into the maker-checker gate: once true, publishing a new
    version or starting a run opens an approval_request instead of acting."""
    sensitivity: str = Field(default="normal", pattern=r"^(normal|elevated)$")
    """'elevated' also gates the workflow (money-moving / high-risk)."""


class WorkflowUpdateRequest(BaseModel):
    """Saves a new version of an existing workflow. Owner-only."""

    model_config = ConfigDict(extra="forbid")
    dag: Dag
    rationale: str = ""


class WorkflowResponse(BaseModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    created_by: uuid.UUID
    name: str
    description: str
    latest_version: int
    requires_approval: bool = False
    sensitivity: str = "normal"
    created_at: datetime
    updated_at: datetime


class WorkflowVersionResponse(BaseModel):
    id: uuid.UUID
    workflow_id: uuid.UUID
    version: int
    dag: Dag
    rationale: str
    created_by: uuid.UUID
    created_at: datetime


# ---------- chat (planner) ------------------------------------------------


class ChatRequest(BaseModel):
    """One turn of NL planning. `current_dag` is set when editing an existing
    workflow; the planner returns a full replacement DAG."""

    model_config = ConfigDict(extra="forbid")
    message: str = Field(min_length=1, max_length=8000)
    current_dag: Dag | None = None
    workflow_id: uuid.UUID | None = None  # if editing a saved workflow


class ChatResponse(BaseModel):
    """Discriminated by `kind` — mirrors PlannerResponse but flat for HTTP."""

    kind: str  # "dag" | "clarify" | "missing"
    rationale: str = ""
    dag: Dag | None = None
    questions: list[str] = Field(default_factory=list)
    needed: list[str] = Field(default_factory=list)
    explanation: str = ""


# ---------- chat sessions -------------------------------------------------


class ChatSessionCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str | None = Field(default=None, max_length=255)


class ChatMessageResponse(BaseModel):
    id: uuid.UUID
    sequence: int
    role: str  # "user" | "planner"
    text: str
    payload: dict[str, Any]
    at: datetime


class ChatSessionResponse(BaseModel):
    """Full session view: header + messages + draft DAG + dirty flag."""

    id: uuid.UUID
    tenant_id: uuid.UUID
    user_id: uuid.UUID
    title: str
    workflow_id: uuid.UUID | None
    saved_version: int | None
    draft_dag: Dag | None
    draft_rationale: str
    is_dirty: bool
    """True iff `draft_dag` differs from the saved workflow version (or no
    workflow has been saved yet but a draft exists)."""
    created_at: datetime
    updated_at: datetime
    messages: list[ChatMessageResponse]


class ChatSessionSummaryResponse(BaseModel):
    """Header-only view for the session list."""

    id: uuid.UUID
    title: str
    workflow_id: uuid.UUID | None
    saved_version: int | None
    is_dirty: bool
    created_at: datetime
    updated_at: datetime


class ChatSendRequest(BaseModel):
    """Append a user turn and run the planner with the full prior history."""

    model_config = ConfigDict(extra="forbid")
    message: str = Field(min_length=1, max_length=8000)


class ChatSaveRequest(BaseModel):
    """Persist the session's draft as a workflow.

    - First save: workflow_id is None on the session, body provides `name`.
      Server creates a new workflow and links it to the session.
    - Subsequent saves: session is bound to a workflow; this issues a
      PATCH (new version) iff the draft differs from `saved_version`.
      `confirm` must be true to apply the update — the UI is expected to
      show a diff first.
    """

    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str = ""
    confirm: bool = False
    """Required for updates (when the session is already bound to a workflow).
    Ignored on first save."""


# ---------- runs ----------------------------------------------------------


class RunStartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int | None = None
    inputs: dict[str, Any] = Field(default_factory=dict)
    target: str | None = Field(
        default=None, pattern=r"^(server|[a-z][a-z0-9_:\-]{0,63})$"
    )
    """Run-level placement chosen at launch: None = use each node's own target;
    "server" = run everything on the API host; an agent alias / pool = run the
    whole workflow there (control nodes always stay on the server)."""
    mode: Literal["live", "dry_run"] = "live"
    """Execution mode. ``dry_run`` walks the full DAG topology but simulates
    side-effecting nodes instead of performing them (no money-moving / no
    irreversible effects); the default ``live`` preserves today's behaviour."""


class RunResponse(BaseModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    workflow_id: uuid.UUID
    workflow_version: int
    started_by: uuid.UUID
    status: str
    mode: str
    started_at: datetime
    ended_at: datetime | None
    outputs: dict[str, Any]
    error: dict[str, Any] | None


class RunEventResponse(BaseModel):
    sequence: int
    node_id: str | None
    kind: str
    payload: dict[str, Any]
    at: datetime


class RunDetailResponse(BaseModel):
    run: RunResponse
    events: list[RunEventResponse]
    pending_prompts: list[PendingPromptResponse]


class PendingPromptResponse(BaseModel):
    node_id: str
    message: str
    expects: str


class RunRespondRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    node_id: str
    response: str


# ---------- governance (maker-checker) ------------------------------------


class ApprovalRequestResponse(BaseModel):
    """A maker-checker approval gate as seen over HTTP."""

    id: uuid.UUID
    tenant_id: uuid.UUID
    subject_type: str
    subject_ref: str
    status: str
    requested_by: uuid.UUID
    requested_at: datetime
    decided_by: uuid.UUID | None
    decided_at: datetime | None
    reason: str
    context: dict[str, Any]


class ApprovalPendingResponse(BaseModel):
    """Returned with HTTP 202 when a gated action is held for approval instead
    of being performed. `approval` is the freshly opened pending request; the
    checker decides it via /approvals/{id}/approve|reject."""

    status: str = "pending_approval"
    approval: ApprovalRequestResponse


class ApprovalDecisionRequest(BaseModel):
    """Body for approve/reject — `reason` is the checker's decision note."""

    model_config = ConfigDict(extra="forbid")
    reason: str = Field(default="", max_length=2000)


# ---------- dashboard / stats ---------------------------------------------


class VolumeBucket(BaseModel):
    """Run-status counts for a fixed time window."""

    queued: int = 0
    running: int = 0
    paused: int = 0
    succeeded: int = 0
    failed: int = 0
    cancelled: int = 0

    @property
    def total(self) -> int:  # pragma: no cover - convenience
        return (
            self.queued
            + self.running
            + self.paused
            + self.succeeded
            + self.failed
            + self.cancelled
        )


class CapabilityUsage(BaseModel):
    capability_ref: str
    count: int
    failure_count: int


class FailureSummary(BaseModel):
    run_id: uuid.UUID
    workflow_id: uuid.UUID
    workflow_name: str
    started_at: datetime
    ended_at: datetime | None = None
    error_type: str
    error_message: str
    tenant_slug: str | None = None
    """Set only on the superuser/global dashboard so the UI can label
    cross-tenant rows; null on tenant-scoped dashboards."""


class TenantVolume(BaseModel):
    """Per-tenant breakdown shown on the superuser dashboard."""

    tenant_id: uuid.UUID
    tenant_slug: str
    tenant_name: str
    total: int
    succeeded: int
    failed: int
    success_rate: float | None
    """succeeded / (succeeded + failed). None if no terminal runs in the window."""


class DailyVolume(BaseModel):
    """One IST-day bucket of run counts grouped by terminal/active status.

    The dashboard renders this as a stacked-area chart; the date is
    yyyy-mm-dd in IST so the chart matches the rest of the UI.
    """

    date: str  # ISO yyyy-mm-dd in IST
    succeeded: int = 0
    failed: int = 0
    paused: int = 0
    running: int = 0
    queued: int = 0
    cancelled: int = 0


class DashboardStatsResponse(BaseModel):
    """Aggregate insights for the role-aware dashboard.

    `scope`:
      - "user"   — the caller's own runs only
      - "tenant" — full tenant view (tenant_admin)
      - "global" — cross-tenant (superuser)
    """

    scope: str
    volume_24h: VolumeBucket
    volume_7d: VolumeBucket
    volume_30d: VolumeBucket
    daily_volume: list[DailyVolume] = Field(default_factory=list)
    """30-day daily run counts (oldest to newest), used for the trend chart."""
    capability_usage: list[CapabilityUsage] = Field(default_factory=list)
    active_count: int
    recent_failures: list[FailureSummary] = Field(default_factory=list)
    per_tenant: list[TenantVolume] | None = None
    """Only set when scope == "global"."""


# ---------- errors --------------------------------------------------------


class ErrorResponse(BaseModel):
    error: str
    detail: str = ""


class AuditEntry(BaseModel):
    """One audit-log record for the audit view."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    action: str
    actor_id: uuid.UUID | None
    target_kind: str
    target_id: str
    payload: dict[str, Any]
    at: datetime


class AuditListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entries: list[AuditEntry]
    total: int


class AuditVerifyResponse(BaseModel):
    """Result of recomputing a tenant's audit hash chain.

    ``ok`` is True iff every chained row's hash and link recompute cleanly.
    ``entries_checked`` is the number of chained (sequenced) rows examined.
    On a break, ``first_broken_seq`` is the ``seq`` of the first failing row
    and ``reason`` is a human-readable description for the auditor.
    """

    model_config = ConfigDict(extra="forbid")

    ok: bool
    entries_checked: int
    first_seq: int | None = None
    last_seq: int | None = None
    first_broken_seq: int | None = None
    reason: str | None = None


class ScheduleCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Exactly one of cron / scheduled_at must be set (enforced in the router).
    cron: str | None = None
    scheduled_at: datetime | None = None
    inputs: dict[str, Any] = Field(default_factory=dict)
    executor_type: str = "local"
    target: str | None = Field(
        default=None, pattern=r"^(server|[a-z][a-z0-9_:\-]{0,63})$"
    )
    """Run-level placement for scheduled runs (same meaning as the launch target)."""


class ScheduleUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool | None = None
    cron: str | None = None
    scheduled_at: datetime | None = None
    inputs: dict[str, Any] | None = None
    target: str | None = Field(
        default=None, pattern=r"^(server|[a-z][a-z0-9_:\-]{0,63})$"
    )


class ScheduleResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    workflow_id: uuid.UUID
    enabled: bool
    cron: str | None
    scheduled_at: datetime | None
    inputs: dict[str, Any]
    executor_type: str
    target: str | None
    created_at: datetime
    last_triggered_at: datetime | None


class AgentEnrollRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    alias: Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_\-]{0,63}$")]
    pools: list[str] = Field(default_factory=list)


class AgentEnrollResponse(BaseModel):
    """Returned once at enrollment. `enrollment_key` is shown a single time."""

    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    alias: str
    agent_id: uuid.UUID
    enrollment_key: str


class AgentCapabilityInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ref: str
    version: str = "1"


class AgentResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    alias: str
    os: str | None
    hostname: str | None
    gui_capable: bool
    pools: list[str]
    capabilities: list[Any]
    agent_version: str | None
    status: str
    last_seen: datetime | None
    created_at: datetime
    online: bool = False


class PlacementIssue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    node_id: str
    ref: str
    target: str
    reason: str


class PlacementCheckResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    issues: list[PlacementIssue]
    online_agents: int
