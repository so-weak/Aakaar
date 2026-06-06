// API endpoint helpers, grouped by resource. Returns parsed JSON or throws ApiError.

import { request } from "./client";
import type {
  AgentEnrollResponse,
  AuditListResponse,
  CapabilityDefinitionResponse,
  ChatSession,
  ChatSessionSummary,
  Dag,
  DashboardStats,
  Grant,
  LoginResponse,
  PlacementCheckResult,
  RawChatResponse,
  RemoteAgent,
  Run,
  RunDetail,
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
};

// ---------- runs ---------------------------------------------------------

export const runs = {
  // `target` is the optional run-level placement override:
  //   undefined / null -> use each node's own placement (default)
  //   "server"         -> run the entire workflow on the API host
  //   "<alias>"/"<pool>" -> run the entire workflow on that agent/pool
  start: (
    workflowId: string,
    inputs: Record<string, unknown> = {},
    target?: string | null,
  ) =>
    request<Run>(`/workflows/${workflowId}/runs`, {
      method: "POST",
      body: { inputs, target: target ?? null },
    }),
  list: (opts: { active?: boolean } = {}) =>
    request<Run[]>(`/runs${opts.active ? "?active=true" : ""}`),
  get: (id: string) => request<RunDetail>(`/runs/${id}`),
  respond: (id: string, input: { node_id: string; response: string }) =>
    request<void>(`/runs/${id}/respond`, { method: "POST", body: input }),
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
  create: (input: { title?: string }) =>
    request<ChatSession>("/chat/sessions", { method: "POST", body: input }),
  list: () => request<ChatSessionSummary[]>("/chat/sessions"),
  get: (id: string) => request<ChatSession>(`/chat/sessions/${id}`),
  remove: (id: string) =>
    request<void>(`/chat/sessions/${id}`, { method: "DELETE" }),
  send: (id: string, input: { message: string }) =>
    request<ChatSession>(`/chat/sessions/${id}/messages`, {
      method: "POST",
      body: input,
    }),
  save: (id: string, input: { name?: string; description?: string; confirm?: boolean }) =>
    request<Workflow>(`/chat/sessions/${id}/save`, {
      method: "POST",
      body: input,
    }),
};
