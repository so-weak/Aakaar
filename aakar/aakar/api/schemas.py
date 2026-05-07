"""HTTP request/response shapes.

Separate from DB models on purpose — they evolve at different cadences and
serve different audiences. Keep these dialect-agnostic and avoid leaking
internal-only fields (e.g. password_hash, vault_ref values).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field

from aakar.shared.dag.types import Dag


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
    access_token: str
    token_type: str = "Bearer"
    expires_at: datetime


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


class RunResponse(BaseModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    workflow_id: uuid.UUID
    workflow_version: int
    started_by: uuid.UUID
    status: str
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
    pending_prompts: list["PendingPromptResponse"]


class PendingPromptResponse(BaseModel):
    node_id: str
    message: str
    expects: str


class RunRespondRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    node_id: str
    response: str


# ---------- errors --------------------------------------------------------


class ErrorResponse(BaseModel):
    error: str
    detail: str = ""
