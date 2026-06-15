# Architecture Decision Records

Short, dated records of the load-bearing architecture choices in Aakaar and the
constraints that forced them. Each ADR states the context, the decision, and the
consequences (including what we gave up). They are immutable once `Accepted`: a
reversal is a new ADR that supersedes an old one, not an edit.

The dominant constraint behind most of these is the **deployment posture**: the
target is a regulated bank that runs Aakaar on a small number of on-prem hosts
with **no third-party infrastructure** (no Postgres cluster, no Redis, no Vault,
no S3, no Temporal/Kafka, no managed KMS) and frequently **no outbound network**
at all. That rules out the usual "just add a managed service" answers and shapes
every decision below.

| ADR | Decision | Status |
|-----|----------|--------|
| [0001](0001-sqlite-as-primary-store.md) | SQLite (not Postgres/Temporal) as the primary store | Accepted |
| [0002](0002-in-process-executor-durable-resume.md) | In-process DAG executor + durable checkpoint/resume | Accepted |
| [0003](0003-app-level-tenancy-optional-rls.md) | Application-level tenant scoping, optional Postgres RLS | Accepted |
| [0004](0004-keyprovider-kms-seam.md) | `KeyProvider` seam for an external KMS without a KMS dependency | Accepted |
| [0005](0005-airgap-posture.md) | First-class airgap posture (lazy heavy deps, offline embeddings) | Accepted |
| [0006](0006-maker-checker-governance.md) | Maker-checker gate decoupled from action execution | Accepted |
| [0007](0007-tamper-evident-audit.md) | Per-tenant hash-chained audit ledger | Accepted |
| [0008](0008-retention-legal-hold-erasure.md) | Tenant retention policies, legal hold, right-to-erasure | Accepted |

## Format

```
# ADR NNNN: <title>
- Status, Date, Deciders
## Context     — the forces in play
## Decision    — what we chose
## Consequences — what follows, good and bad
```
