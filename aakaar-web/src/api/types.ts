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
  // Absent when a second factor is still required — the server returns an
  // `mfa_token` ticket instead and the user must complete /auth/mfa/verify.
  access_token: z.string().nullable().optional(),
  token_type: z.string(),
  expires_at: z.string().nullable().optional(),
  tenant_slug: z.string().nullable().optional(),
  tenant_name: z.string().nullable().optional(),
  mfa_required: z.boolean(),
  mfa_token: z.string().nullable().optional(),
});
export type LoginResponse = z.infer<typeof LoginResponseSchema>;

export interface MfaStatus {
  enabled: boolean;
  pending: boolean;
}

export interface MfaEnrollResponse {
  secret: string;
  otpauth_url: string;
}

export interface MfaConfirmResponse {
  recovery_codes: string[];
}

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

// Result of recomputing a tenant's audit hash chain. `ok` is true iff every
// chained row's hash and prev-link recompute cleanly; on a break,
// `first_broken_seq` points at the first failing row and `reason` explains it.
export interface AuditVerifyResponse {
  ok: boolean;
  entries_checked: number;
  first_seq: number | null;
  last_seq: number | null;
  first_broken_seq: number | null;
  reason: string | null;
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

// 'live' executes for real; 'dry_run' walks the DAG but simulates
// side-effecting (money-moving / irreversible) nodes instead of performing
// them. Chosen at launch; the run row carries it back.
export type RunMode = "live" | "dry_run";

export interface Run {
  id: string;
  tenant_id: string;
  workflow_id: string;
  workflow_version: number;
  started_by: string;
  status: RunStatus;
  mode: RunMode;
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

// ---------- governance / maker-checker -----------------------------------

export type ApprovalStatus = "pending" | "approved" | "rejected" | "cancelled";

// What kind of action a request gates. The backend uses these string values
// (ApprovalSubjectType) verbatim; subject_ref is the gated resource (the
// workflow id for both publish and run-start gates).
export type ApprovalSubjectType = "workflow_publish" | "run_start";

export interface ApprovalRequest {
  id: string;
  tenant_id: string;
  subject_type: ApprovalSubjectType | string;
  subject_ref: string;
  status: ApprovalStatus;
  requested_by: string;
  requested_at: string;
  decided_by: string | null;
  decided_at: string | null;
  reason: string;
  // Frozen snapshot the checker needs to decide (workflow_name, version,
  // inputs, run_target/mode for run-start; workflow_id/version for publish).
  context: Record<string, unknown>;
}

// Returned with HTTP 202 when a gated run-start / publish is held for approval
// instead of being performed. Narrow a run-start result on the `status` field.
export interface ApprovalPendingResponse {
  status: "pending_approval";
  approval: ApprovalRequest;
}

// A run-start either launches (201 -> Run) or opens a maker-checker gate
// (202 -> ApprovalPendingResponse). The caller branches on the discriminator.
export type RunStartResult = Run | ApprovalPendingResponse;

export function isApprovalPending(
  r: RunStartResult,
): r is ApprovalPendingResponse {
  return (r as ApprovalPendingResponse).status === "pending_approval";
}

// ---------- activity recordings -------------------------------------------
// Tenant-admin only. Recording state lives in server memory: a restart
// forgets in-flight recordings, and stop/discard remove the entry entirely.

export interface RecordingStartResponse {
  recording_id: string;
  status: "recording";
  name: string;
  agent_alias: string;
  event_count: number;
  started_at: string;
  expires_at: string;
  // Server-authored text explaining keystroke redaction; surface it verbatim.
  privacy_note: string;
}

export interface RecordingListItem {
  recording_id: string;
  status: "recording";
  name: string;
  agent_alias: string;
  started_at: string;
  expires_at: string;
}

export interface RecordingStatus {
  recording_id: string;
  // Agent-reported; normally "recording".
  status: string;
  name: string;
  agent_alias: string;
  event_count: number;
  duration_seconds: number;
  started_at: string;
  expires_at: string;
}

export interface RecordingStopResponse {
  recording_id: string;
  status: "stopped";
  event_count: number;
  // The draft workflow created from the capture — link the user here.
  workflow_id: string;
  workflow_name: string;
  draft_dag: Dag;
  // Human-readable caveats (truncation, coordinate clicks, redacted-text
  // placeholders that MUST be replaced before the draft is runnable).
  warnings: string[];
  rationale: string;
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

// ---------- retention / legal-hold / erasure ------------------------------
// Tenant-admin only. The resource types the backend can age out / hold / erase
// (ERASABLE_RESOURCE_TYPES). Kept as a string union for the policy + hold forms.

export type RetentionResourceType = "run" | "stored_object";

export interface RetentionPolicy {
  resource_type: string;
  // Days to retain; null = keep indefinitely.
  ttl_days: number | null;
  updated_at: string;
  updated_by: string | null;
}

export interface EraseResponse {
  resource_type: string;
  resource_id: string;
  erased_at: string;
  // True when the resource was already erased (idempotent re-request).
  already_erased: boolean;
}
