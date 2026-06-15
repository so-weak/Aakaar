# ADR 0006: Maker-checker gate decoupled from action execution

- **Status:** Accepted
- **Date:** 2026-06-15
- **Deciders:** Platform engineering, Risk/Controls

## Context

A bank requires **segregation of duties (SoD)**: a person who initiates a
sensitive change must not be the one who approves it. Two Aakaar actions warrant
a maker-checker gate — **publishing** a workflow version (it becomes runnable)
and **starting a run** of a sensitive workflow (it touches external systems). The
control must be auditable and impossible to self-approve, and it must not bleed
router/orchestrator logic into the decision core (which would make it hard to
test and reason about).

## Decision

A dedicated **governance service** (`aakaar/aakaar/services/governance/service.py`)
owns only the *decision*, not the *action*:

- A workflow is gated when it opts in (`requires_approval=True`) or is marked
  `sensitivity='elevated'` (`workflow_is_gated`). The default
  (`requires_approval=False`, `sensitivity='normal'`) preserves existing
  behaviour, so non-sensitive flows are unaffected.
- When a gated publish/run-start is attempted, the API does **not** execute. It
  snapshots what the action needs (`GatedAction.context`) and opens a pending
  `ApprovalRequest` via `open_gate`, returning **HTTP 202 Accepted** with the
  pending approval. Publishing a new workflow version is `PATCH
  /workflows/{workflow_id}` (it returns `WorkflowVersionResponse |
  ApprovalPendingResponse`); starting a run is `POST /workflows/{workflow_id}/runs`.
- A *different* tenant admin — the checker — decides it at
  `POST /approvals/{id}/approve` or `/reject`. The one rule the service
  guarantees is SoD: the approver must not be the requester
  (`SelfApprovalError` -> **409**).
- **On approval, the API (not the service) performs the originally-gated action**
  under the checker's authorization, attributed to the original maker. The
  decision is committed *before* the action runs, so an action that fails after
  the decision (e.g. the pinned version was deleted) surfaces as 409 with an
  audited `approved` request, never a silently-lost approval.

Every decision is written to the tamper-evident audit ledger
(`approval.approve` / `approval.reject`, ADR 0007).

## Consequences

**Positive**

- The decision core has no router/orchestrator imports — it is trivially
  unit-testable, and the SoD invariant lives in exactly one place.
- "Decide, commit, then perform" makes the audit trail the source of truth: an
  approval is durable even if performing the action later fails.
- Listing is tenant-scoped and visible to any tenant user (so a maker can watch
  their own request); only admins decide.

**Negative / accepted trade-offs**

- **Two-step latency** for gated actions (open gate -> human approves -> perform).
  This is the point of the control, not a defect, but operators must understand a
  202 means "pending", not "done".
- An approved action performed later runs against *current* state, not the state
  at request time (the DAG is re-read from the pinned version) — a deleted
  version becomes a 409 to the approver rather than executing stale.
- Gating is per-workflow policy (`requires_approval` / `sensitivity`); it is not a
  global "approve everything" switch.

## Alternatives considered

- **Perform-then-record inside the service.** Rejected: couples the decision core
  to execution and risks performing without a durable decision.
- **Approve at run time via a flag with no second user.** Rejected: that is not
  segregation of duties.
