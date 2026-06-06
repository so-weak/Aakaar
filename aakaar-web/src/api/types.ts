// Backend response shapes — kept in sync with aakaar/api/schemas.py.
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
  // Placement: "server"/null runs on the API host; any other value is a
  // remote agent alias or pool label that routes the node to an agent.
  target?: string | null;
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
  tenant_slug: z.string().nullable().optional(),
  tenant_name: z.string().nullable().optional(),
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

// ---------- schedules ----------------------------------------------------

export interface WorkflowSchedule {
  id: string;
  workflow_id: string;
  enabled: boolean;
  cron: string | null;
  scheduled_at: string | null;
  inputs: Record<string, unknown>;
  executor_type: string;
  created_at: string;
  last_triggered_at: string | null;
}

// ---------- audit --------------------------------------------------------

export interface AuditEntry {
  id: string;
  action: string;
  actor_id: string | null;
  target_kind: string;
  target_id: string;
  payload: Record<string, unknown>;
  at: string;
}

export interface AuditListResponse {
  entries: AuditEntry[];
  total: number;
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

// ---------- chat sessions ------------------------------------------------

export interface ChatMessage {
  id: string;
  sequence: number;
  role: "user" | "planner";
  text: string;
  payload: RawChatResponse | Record<string, never>;
  at: string;
}

export interface ChatSessionSummary {
  id: string;
  title: string;
  workflow_id: string | null;
  saved_version: number | null;
  is_dirty: boolean;
  created_at: string;
  updated_at: string;
}

export interface ChatSession {
  id: string;
  tenant_id: string;
  user_id: string;
  title: string;
  workflow_id: string | null;
  saved_version: number | null;
  draft_dag: Dag | null;
  draft_rationale: string;
  is_dirty: boolean;
  created_at: string;
  updated_at: string;
  messages: ChatMessage[];
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

// ---------- remote agents ------------------------------------------------

export interface AgentCapabilityInfo {
  ref: string;
  version: string;
}

export interface RemoteAgent {
  id: string;
  alias: string;
  os: string | null;
  hostname: string | null;
  gui_capable: boolean;
  pools: string[];
  capabilities: AgentCapabilityInfo[];
  agent_version: string | null;
  status: string;
  last_seen: string | null;
  created_at: string;
  online: boolean;
}

export interface AgentEnrollResponse {
  id: string;
  alias: string;
  agent_id: string;
  // Shown to the operator exactly once — there is no way to retrieve it later.
  enrollment_key: string;
}

// ---------- placement ----------------------------------------------------

export interface PlacementIssue {
  node_id: string;
  ref: string;
  target: string;
  reason: string;
}

export interface PlacementCheckResult {
  issues: PlacementIssue[];
  online_agents: number;
}

// ---------- dashboard / stats -------------------------------------------

export interface VolumeBucket {
  queued: number;
  running: number;
  paused: number;
  succeeded: number;
  failed: number;
  cancelled: number;
}

export interface CapabilityUsage {
  capability_ref: string;
  count: number;
  failure_count: number;
}

export interface FailureSummary {
  run_id: string;
  workflow_id: string;
  workflow_name: string;
  started_at: string;
  ended_at: string | null;
  error_type: string;
  error_message: string;
  tenant_slug: string | null;
}

export interface TenantVolume {
  tenant_id: string;
  tenant_slug: string;
  tenant_name: string;
  total: number;
  succeeded: number;
  failed: number;
  success_rate: number | null;
}

export interface DailyVolume {
  date: string; // ISO yyyy-mm-dd in IST
  succeeded: number;
  failed: number;
  paused: number;
  running: number;
  queued: number;
  cancelled: number;
}

export type DashboardScope = "user" | "tenant" | "global";

export interface DashboardStats {
  scope: DashboardScope;
  volume_24h: VolumeBucket;
  volume_7d: VolumeBucket;
  volume_30d: VolumeBucket;
  daily_volume: DailyVolume[];
  capability_usage: CapabilityUsage[];
  active_count: number;
  recent_failures: FailureSummary[];
  per_tenant: TenantVolume[] | null;
}
