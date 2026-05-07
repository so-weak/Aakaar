// Backend response shapes — kept in sync with aakar/api/schemas.py.
//
// We use Zod for the few fields where parsing matters (auth, planner
// responses); plain interfaces are fine for the rest because the API only
// returns shapes we control.

import { z } from "zod";

// ---------- DAG ----------------------------------------------------------

export type NodeKind = "capability" | "action" | "control";

export interface DagNode {
  id: string;
  kind: NodeKind;
  ref: string;
  inputs: Record<string, unknown>;
  outputs_as: string | null;
}

export interface DagEdge {
  from: string;
  to: string;
}

export interface Dag {
  id: string;
  version: number;
  nodes: DagNode[];
  edges: DagEdge[];
}

// ---------- auth ---------------------------------------------------------

export const LoginResponseSchema = z.object({
  access_token: z.string(),
  token_type: z.string(),
  expires_at: z.string(),
});
export type LoginResponse = z.infer<typeof LoginResponseSchema>;

export const TokenClaimsSchema = z.object({
  user_id: z.string(),
  tenant_id: z.string().nullable(),
  role: z.enum(["superuser", "tenant_admin", "tenant_user"]),
  expires_at: z.number(),
});
export type TokenClaims = z.infer<typeof TokenClaimsSchema>;

// ---------- tenants / users ---------------------------------------------

export interface Tenant {
  id: string;
  slug: string;
  name: string;
  status: string;
  created_at: string;
}

export interface User {
  id: string;
  tenant_id: string | null;
  email: string;
  role: "superuser" | "tenant_admin" | "tenant_user";
  status: string;
  created_at: string;
}

// ---------- grants / capabilities ---------------------------------------

export interface Grant {
  id: string;
  capability_ref: string;
  account_alias: string;
  secret_names: string[];
  input_defaults: Record<string, unknown>;
  enabled: boolean;
  created_at: string;
}

export interface CapabilityFieldInfo {
  name: string;
  type_label: string;
  required: boolean;
  description: string;
}

export interface CapabilityDefinitionResponse {
  ref: string;
  kind: NodeKind;
  description: string;
  inputs: CapabilityFieldInfo[];
  outputs: CapabilityFieldInfo[];
  secret_names: string[];
  tags: string[];
}

// ---------- workflows ----------------------------------------------------

export interface Workflow {
  id: string;
  tenant_id: string;
  created_by: string;
  name: string;
  description: string;
  latest_version: number;
  created_at: string;
  updated_at: string;
}

export interface WorkflowVersion {
  id: string;
  workflow_id: string;
  version: number;
  dag: Dag;
  rationale: string;
  created_by: string;
  created_at: string;
}

// ---------- chat ---------------------------------------------------------

export type ChatResponse =
  | { kind: "dag"; rationale: string; dag: Dag; questions: never[]; needed: never[]; explanation: "" }
  | { kind: "clarify"; questions: string[]; rationale: string; dag: null; needed: never[]; explanation: "" }
  | { kind: "missing"; needed: string[]; explanation: string; rationale: string; dag: null; questions: never[] };

// Server returns a flat shape; narrow at the call site.
export interface RawChatResponse {
  kind: "dag" | "clarify" | "missing";
  rationale: string;
  dag: Dag | null;
  questions: string[];
  needed: string[];
  explanation: string;
}

// ---------- runs ---------------------------------------------------------

export type RunStatus =
  | "queued"
  | "running"
  | "paused"
  | "succeeded"
  | "failed"
  | "cancelled";

export interface Run {
  id: string;
  tenant_id: string;
  workflow_id: string;
  workflow_version: number;
  started_by: string;
  status: RunStatus;
  started_at: string;
  ended_at: string | null;
  outputs: Record<string, Record<string, unknown>>;
  error: { type: string; message: string } | null;
}

export interface RunEvent {
  sequence: number;
  node_id: string | null;
  kind: string;
  payload: Record<string, unknown>;
  at: string;
}

export interface PendingPrompt {
  node_id: string;
  message: string;
  expects: "text" | "otp" | "confirm";
}

export interface RunDetail {
  run: Run;
  events: RunEvent[];
  pending_prompts: PendingPrompt[];
}
