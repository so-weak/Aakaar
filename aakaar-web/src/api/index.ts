// API endpoint helpers, grouped by resource. Returns parsed JSON or throws ApiError.

import { request } from "./client";
import type {
  AgentEnrollResponse,
  ApprovalRequest,
  ApprovalStatus,
  AuditListResponse,
  AuditVerifyResponse,
  CapabilityDefinitionResponse,
  ChatSession,
  ChatSessionSummary,
  Dag,
  DashboardStats,
  EraseResponse,
  Grant,
  LoginResponse,
  MfaConfirmResponse,
  MfaEnrollResponse,
  MfaStatus,
  PlacementCheckResult,
  RawChatResponse,
  RecordingListItem,
  RecordingStartResponse,
  RecordingStatus,
  RecordingStopResponse,
  RemoteAgent,
  RetentionPolicy,
  Run,
  RunDetail,
  RunMode,
  RunStartResult,
  Tenant,
  User,
  Workflow,
  WorkflowSchedule,
  WorkflowVersion,
} from "./types";

// ---------- auth ---------------------------------------------------------

export const auth = {
  login: (email: string, password: string) =>
    request<LoginResponse>("/auth/login", { method: "POST", body: { email, password } }),
  // Second factor: send exactly one of `code` (TOTP) or `recovery_code`.
  mfaVerify: (input: { mfa_token: string; code?: string; recovery_code?: string }) =>
    request<LoginResponse>("/auth/mfa/verify", { method: "POST", body: input }),
  mfaStatus: () => request<MfaStatus>("/auth/mfa/status"),
  mfaEnroll: () => request<MfaEnrollResponse>("/auth/mfa/enroll", { method: "POST" }),
  mfaConfirm: (code: string) =>
    request<MfaConfirmResponse>("/auth/mfa/confirm", { method: "POST", body: { code } }),
  mfaDisable: (code: string) =>
    request<void>("/auth/mfa/disable", { method: "POST", body: { code } }),
};

// ---------- superuser ----------------------------------------------------

export const superuser = {
  listTenants: () => request<Tenant[]>("/superuser/tenants"),
  createTenant: (input: {
    slug: string;
    name: string;
    admin_email: string;
    admin_password: string;
  }) => request<Tenant>("/superuser/tenants", { method: "POST", body: input }),
  suspendTenant: (id: string) =>
    request<Tenant>(`/superuser/tenants/${id}/suspend`, { method: "POST" }),
  listTenantUsers: (id: string) => request<User[]>(`/superuser/tenants/${id}/users`),
  listAllUsers: () => request<User[]>("/superuser/users"),
  listAllRuns: (opts: { active?: boolean } = {}) =>
    request<Run[]>(`/superuser/runs${opts.active ? "?active=true" : ""}`),
  getRunDetail: (id: string) => request<RunDetail>(`/superuser/runs/${id}`),
  // Cross-tenant run lifecycle for the operator console. Mirror the tenant-level
  // runs.pause/resume/cancel but skip the owner check. cancel is cooperative —
  // the returned status may still be pre-terminal, so keep polling.
  pauseRun: (id: string) =>
    request<Run>(`/superuser/runs/${id}/pause`, { method: "POST" }),
  resumeRun: (id: string) =>
    request<Run>(`/superuser/runs/${id}/resume`, { method: "POST" }),
  cancelRun: (id: string) =>
    request<Run>(`/superuser/runs/${id}/cancel`, { method: "POST" }),
  getWorkflow: (id: string) =>
    request<Workflow>(`/superuser/workflows/${id}`),
  getWorkflowVersion: (id: string, version: number) =>
    request<WorkflowVersion>(`/superuser/workflows/${id}/versions/${version}`),
  listTenantGrants: (id: string) => request<Grant[]>(`/superuser/tenants/${id}/grants`),
  createTenantGrant: (
    id: string,
    input: {
      capability_ref: string;
      account_alias: string;
      secrets: Record<string, string>;
      input_defaults?: Record<string, unknown>;
    },
  ) => request<Grant>(`/superuser/tenants/${id}/grants`, { method: "POST", body: input }),
  updateTenantGrant: (
    tenantId: string,
    grantId: string,
    input: {
      account_alias?: string;
      secrets?: Record<string, string>;
      input_defaults?: Record<string, unknown>;
      enabled?: boolean;
    },
  ) =>
    request<Grant>(`/superuser/tenants/${tenantId}/grants/${grantId}`, {
      method: "PATCH",
      body: input,
    }),
  deleteTenantGrant: (tenantId: string, grantId: string) =>
    request<void>(`/superuser/tenants/${tenantId}/grants/${grantId}`, {
      method: "DELETE",
    }),
  getDashboard: () => request<DashboardStats>("/superuser/stats/dashboard"),
};

// ---------- stats --------------------------------------------------------

export const stats = {
  getDashboard: () => request<DashboardStats>("/stats/dashboard"),
};

// ---------- admin --------------------------------------------------------

export const admin = {
  listUsers: () => request<User[]>("/admin/users"),
  createUser: (input: { email: string; password: string; role: "tenant_admin" | "tenant_user" }) =>
    request<User>("/admin/users", { method: "POST", body: input }),
  updateUser: (
    id: string,
    input: { role?: "tenant_admin" | "tenant_user"; password?: string },
  ) => request<User>(`/admin/users/${id}`, { method: "PATCH", body: input }),
  suspendUser: (id: string) =>
    request<User>(`/admin/users/${id}/suspend`, { method: "POST" }),
  reactivateUser: (id: string) =>
    request<User>(`/admin/users/${id}/reactivate`, { method: "POST" }),
  listGrants: () => request<Grant[]>("/admin/grants"),
  createGrant: (input: {
    capability_ref: string;
    account_alias: string;
    secrets: Record<string, string>;
    input_defaults?: Record<string, unknown>;
  }) => request<Grant>("/admin/grants", { method: "POST", body: input }),
  updateGrant: (
    id: string,
    input: {
      account_alias?: string;
      secrets?: Record<string, string>;
      input_defaults?: Record<string, unknown>;
      enabled?: boolean;
    },
  ) => request<Grant>(`/admin/grants/${id}`, { method: "PATCH", body: input }),
  deleteGrant: (id: string) =>
    request<void>(`/admin/grants/${id}`, { method: "DELETE" }),
};

// ---------- capabilities -------------------------------------------------

export const capabilities = {
  list: () => request<CapabilityDefinitionResponse[]>("/capabilities"),
  listAll: () => request<CapabilityDefinitionResponse[]>("/capabilities/all"),
};

// ---------- workflows ----------------------------------------------------

export const workflows = {
  list: () => request<Workflow[]>("/workflows"),
  create: (input: { name: string; description?: string; dag: Dag; rationale?: string }) =>
    request<Workflow>("/workflows", { method: "POST", body: input }),
  get: (id: string) => request<Workflow>(`/workflows/${id}`),
  getLatestVersion: (id: string) =>
    request<WorkflowVersion>(`/workflows/${id}/versions/latest`),
  getVersion: (id: string, version: number) =>
    request<WorkflowVersion>(`/workflows/${id}/versions/${version}`),
  update: (id: string, input: { dag: Dag; rationale?: string }) =>
    request<WorkflowVersion>(`/workflows/${id}`, { method: "PATCH", body: input }),
  remove: (id: string) => request<void>(`/workflows/${id}`, { method: "DELETE" }),
};

// ---------- schedules ----------------------------------------------------

export const schedules = {
  list: (workflowId: string) =>
    request<WorkflowSchedule[]>(`/workflows/${workflowId}/schedules`),
  create: (
    workflowId: string,
    input: {
      cron?: string | null;
      scheduled_at?: string | null;
      inputs?: Record<string, unknown>;
      executor_type?: "local";
      // Run-level placement override; see runs.start for the semantics.
      target?: string | null;
    },
  ) =>
    request<WorkflowSchedule>(`/workflows/${workflowId}/schedules`, {
      method: "POST",
      body: input,
    }),
  update: (
    scheduleId: string,
    input: {
      enabled?: boolean;
      cron?: string | null;
      scheduled_at?: string | null;
      inputs?: Record<string, unknown>;
    },
  ) =>
    request<WorkflowSchedule>(`/schedules/${scheduleId}`, {
      method: "PATCH",
      body: input,
    }),
  remove: (scheduleId: string) =>
    request<void>(`/schedules/${scheduleId}`, { method: "DELETE" }),
};

// ---------- audit --------------------------------------------------------

export const audit = {
  list: (params: { limit?: number; offset?: number; action_prefix?: string } = {}) =>
    request<AuditListResponse>("/audit", {
      query: {
        limit: params.limit,
        offset: params.offset,
        action_prefix: params.action_prefix || undefined,
      },
    }),
  // Recompute the calling tenant's audit hash chain end-to-end (tamper check).
  verify: () => request<AuditVerifyResponse>("/audit/verify"),
  // Stream the chain as JSONL for offline re-verification. We fetch it as a
  // blob with the bearer token (a plain <a download> can't send Authorization),
  // mirroring useObjectBlob; the caller triggers the browser download.
  exportBlob: async (): Promise<Blob> => {
    const base = (import.meta.env.VITE_API_BASE as string | undefined) ?? "/api";
    const token = sessionStorage.getItem("aakaar.token") ?? "";
    const res = await fetch(`${base}/audit/export`, {
      headers: { Authorization: `Bearer ${token}`, Accept: "application/x-ndjson" },
    });
    if (!res.ok) {
      throw new Error(`audit export failed: HTTP ${res.status}`);
    }
    return res.blob();
  },
};

// ---------- approvals (maker-checker) ------------------------------------

export const approvals = {
  list: (params: { status?: ApprovalStatus; limit?: number } = {}) =>
    request<ApprovalRequest[]>("/approvals", {
      query: { status: params.status, limit: params.limit },
    }),
  get: (id: string) => request<ApprovalRequest>(`/approvals/${id}`),
  // approve PERFORMS the gated action (publish / run-start) under the checker's
  // authorization; reject only records the decision. Both are tenant-admin only
  // and the approver may not be the maker (409 SelfApprovalError otherwise).
  approve: (id: string, reason = "") =>
    request<ApprovalRequest>(`/approvals/${id}/approve`, {
      method: "POST",
      body: { reason },
    }),
  reject: (id: string, reason = "") =>
    request<ApprovalRequest>(`/approvals/${id}/reject`, {
      method: "POST",
      body: { reason },
    }),
};

// ---------- retention / legal-hold / erasure -----------------------------

export const retention = {
  listPolicies: () => request<RetentionPolicy[]>("/retention/policies"),
  // ttl_days: null = retain indefinitely; >= 1 otherwise.
  putPolicy: (resourceType: string, ttlDays: number | null) =>
    request<RetentionPolicy>(`/retention/policies/${resourceType}`, {
      method: "PUT",
      body: { ttl_days: ttlDays },
    }),
  // Set or clear a legal hold (204). 404 if the resource is absent/cross-tenant.
  setLegalHold: (input: { resource_type: string; resource_id: string; hold: boolean }) =>
    request<void>("/retention/legal-hold", { method: "POST", body: input }),
  // Right-to-erasure for one resource. 409 if under legal hold; 404 if absent.
  erase: (input: { resource_type: string; resource_id: string; reason?: string }) =>
    request<EraseResponse>("/retention/erase", { method: "POST", body: input }),
};

// ---------- runs ---------------------------------------------------------

export const runs = {
  // `target` is the optional run-level placement override:
  //   undefined / null -> use each node's own placement (default)
  //   "server"         -> run the entire workflow on the API host
  //   "<alias>"/"<pool>" -> run the entire workflow on that agent/pool
  // `version` pins the run to a specific workflow version (default: latest).
  // `mode` chooses live (default) or dry_run execution.
  //
  // Returns a Run on a normal launch (201) OR an ApprovalPendingResponse when
  // the workflow is gated and a maker-checker approval was opened (202) —
  // narrow with `isApprovalPending` at the call site.
  start: (
    workflowId: string,
    inputs: Record<string, unknown> = {},
    target?: string | null,
    version?: number | null,
    mode: RunMode = "live",
  ) =>
    request<RunStartResult>(`/workflows/${workflowId}/runs`, {
      method: "POST",
      body: { inputs, target: target ?? null, version: version ?? null, mode },
    }),
  list: (opts: { active?: boolean } = {}) =>
    request<Run[]>(`/runs${opts.active ? "?active=true" : ""}`),
  get: (id: string) => request<RunDetail>(`/runs/${id}`),
  respond: (id: string, input: { node_id: string; response: string }) =>
    request<void>(`/runs/${id}/respond`, { method: "POST", body: input }),
  // Lifecycle controls (starter or tenant admin; rerun is open to any tenant
  // user). All are bodyless POSTs returning the run row. Note: cancel is
  // cooperative — the 200 may still show a pre-terminal status, so callers
  // should keep polling until status === "cancelled".
  pause: (id: string) => request<Run>(`/runs/${id}/pause`, { method: "POST" }),
  resume: (id: string) => request<Run>(`/runs/${id}/resume`, { method: "POST" }),
  cancel: (id: string) => request<Run>(`/runs/${id}/cancel`, { method: "POST" }),
  // Returns the NEW run, pinned to the source run's version and inputs.
  rerun: (id: string) => request<Run>(`/runs/${id}/rerun`, { method: "POST" }),
};

// ---------- activity recordings -------------------------------------------

export const recordings = {
  start: (input: { name: string; agent_alias: string; max_events?: number }) =>
    request<RecordingStartResponse>("/recordings", { method: "POST", body: input }),
  list: () => request<RecordingListItem[]>("/recordings"),
  status: (id: string) => request<RecordingStatus>(`/recordings/${id}`),
  // Compiles the capture into a draft workflow and removes the recording.
  stop: (id: string) =>
    request<RecordingStopResponse>(`/recordings/${id}/stop`, { method: "POST" }),
  discard: (id: string) => request<void>(`/recordings/${id}`, { method: "DELETE" }),
};

// ---------- remote agents ------------------------------------------------

export const agents = {
  list: () => request<RemoteAgent[]>("/agents"),
  enroll: (input: { alias: string; pools: string[] }) =>
    request<AgentEnrollResponse>("/agents/enroll", { method: "POST", body: input }),
  revoke: (id: string) => request<void>(`/agents/${id}`, { method: "DELETE" }),
};

// ---------- placement ----------------------------------------------------

export const placement = {
  check: (dag: Dag) =>
    request<PlacementCheckResult>("/placement/check", { method: "POST", body: dag }),
};

// ---------- chat ---------------------------------------------------------

export const chat = {
  send: (input: { message: string; current_dag?: Dag | null; workflow_id?: string | null }) =>
    request<RawChatResponse>("/chat", { method: "POST", body: input }),
};

// ---------- chat sessions ------------------------------------------------

export const chatSessions = {
  // `workflow_id` opens a "Refine: <name>" session pre-loaded with that
  // workflow's latest version (owner-only); the returned session carries a
  // `composer_seed` to prefill the message box.
  create: (input: { title?: string; workflow_id?: string }) =>
    request<ChatSession>("/chat/sessions", { method: "POST", body: input }),
  list: () => request<ChatSessionSummary[]>("/chat/sessions"),
  get: (id: string) => request<ChatSession>(`/chat/sessions/${id}`),
  remove: (id: string) =>
    request<void>(`/chat/sessions/${id}`, { method: "DELETE" }),
  // `signal` lets the caller abort an in-flight planner turn (the composer's
  // Stop button wires an AbortController here). The fetch rejects with a
  // DOMException("AbortError"), which the caller swallows rather than surfacing.
  send: (id: string, input: { message: string }, signal?: AbortSignal) =>
    request<ChatSession>(`/chat/sessions/${id}/messages`, {
      method: "POST",
      body: input,
      signal,
    }),
  save: (id: string, input: { name?: string; description?: string; confirm?: boolean }) =>
    request<Workflow>(`/chat/sessions/${id}/save`, {
      method: "POST",
      body: input,
    }),
  // Push a hand-edited draft DAG back to the session (Advanced JSON editor).
  // The DAG is validated server-side; a malformed graph is rejected (422).
  updateDraft: (id: string, dag: Dag) =>
    request<ChatSession>(`/chat/sessions/${id}/draft`, {
      method: "PUT",
      body: dag,
    }),
};
