// API endpoint helpers, grouped by resource. Returns parsed JSON or throws ApiError.

import { request } from "./client";
import type {
  CapabilityDefinitionResponse,
  Dag,
  Grant,
  LoginResponse,
  RawChatResponse,
  Run,
  RunDetail,
  Tenant,
  User,
  Workflow,
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
  deleteTenantGrant: (tenantId: string, grantId: string) =>
    request<void>(`/superuser/tenants/${tenantId}/grants/${grantId}`, {
      method: "DELETE",
    }),
};

// ---------- admin --------------------------------------------------------

export const admin = {
  listUsers: () => request<User[]>("/admin/users"),
  createUser: (input: { email: string; password: string; role: "tenant_admin" | "tenant_user" }) =>
    request<User>("/admin/users", { method: "POST", body: input }),
  listGrants: () => request<Grant[]>("/admin/grants"),
  createGrant: (input: {
    capability_ref: string;
    account_alias: string;
    secrets: Record<string, string>;
    input_defaults?: Record<string, unknown>;
  }) => request<Grant>("/admin/grants", { method: "POST", body: input }),
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
  update: (id: string, input: { dag: Dag; rationale?: string }) =>
    request<WorkflowVersion>(`/workflows/${id}`, { method: "PATCH", body: input }),
  remove: (id: string) => request<void>(`/workflows/${id}`, { method: "DELETE" }),
};

// ---------- runs ---------------------------------------------------------

export const runs = {
  start: (workflowId: string, inputs: Record<string, unknown> = {}) =>
    request<Run>(`/workflows/${workflowId}/runs`, {
      method: "POST",
      body: { inputs },
    }),
  list: () => request<Run[]>("/runs"),
  get: (id: string) => request<RunDetail>(`/runs/${id}`),
  respond: (id: string, input: { node_id: string; response: string }) =>
    request<void>(`/runs/${id}/respond`, { method: "POST", body: input }),
};

// ---------- chat ---------------------------------------------------------

export const chat = {
  send: (input: { message: string; current_dag?: Dag | null; workflow_id?: string | null }) =>
    request<RawChatResponse>("/chat", { method: "POST", body: input }),
};
