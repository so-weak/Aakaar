"""governance, durable runs, tamper-evident audit, retention, human tasks

Stage-1 schema foundation for five feature areas. Everything is additive:
new columns are nullable or carry a server_default, and new tables are created
fresh — so this applies cleanly on a populated SQLite or Postgres DB.

Columns added
  * runs              — mode, checkpoint, resume_count, legal_hold, erased_at
  * run_events        — published, published_at (event outbox) + outbox index
  * audit_log         — seq, prev_hash, entry_hash (hash chain) + (tenant,seq)
  * workflows         — requires_approval, sensitivity
  * workflow_versions — requires_approval, sensitivity

Tables added
  * run_checkpoints     — per-layer crash-safe resume snapshots
  * approval_requests   — maker-checker governance gate
  * retention_policies  — per-tenant/per-resource retention rules
  * stored_objects      — DB metadata for object-store artifacts (retention /
                          legal hold / erasure handles)
  * human_tasks         — persisted, SLA-bounded human-in-the-loop prompts

RLS: the five new tenant-scoped tables get the same fail-closed CASE policy as
0007 (Postgres only; no-op on SQLite) so the FORCE-RLS isolation extends to
them. SQLite relies on the tenancy contextvar as before.

Revision ID: 0008
Revises: 0007
Create Date: 2026-06-14
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# ---- RLS predicate (mirrors 0007 exactly) ---------------------------------
# A CASE, not OR: Postgres does not guarantee OR short-circuits, so the ::uuid
# cast must only run for a real-UUID marker. 'system' => all rows; '' => none.
def _match(expr: str) -> str:
    return (
        "CASE current_setting('app.tenant_id', true) "
        "WHEN 'system' THEN true "
        "WHEN '' THEN false "
        f"ELSE {expr} = current_setting('app.tenant_id', true)::uuid END"
    )


_NEW_TENANT_TABLES = (
    "run_checkpoints",
    "approval_requests",
    "retention_policies",
    "stored_objects",
    "human_tasks",
)


def _enable_rls(table: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    pred = _match("tenant_id")
    name = f"{table}_tenant_isolation"
    op.execute(f"DROP POLICY IF EXISTS {name} ON {table}")
    op.execute(f"CREATE POLICY {name} ON {table} USING ({pred}) WITH CHECK ({pred})")


def upgrade() -> None:
    # ---- 1. durable runs: columns on runs ----------------------------------
    op.add_column(
        "runs",
        sa.Column("mode", sa.String(16), nullable=False, server_default="live"),
    )
    op.add_column("runs", sa.Column("checkpoint", sa.JSON(), nullable=True))
    op.add_column(
        "runs",
        sa.Column("resume_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "runs",
        sa.Column("legal_hold", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column("runs", sa.Column("erased_at", sa.DateTime(timezone=True), nullable=True))

    # ---- 1b. event outbox: columns on run_events ---------------------------
    op.add_column(
        "run_events",
        sa.Column("published", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "run_events",
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_run_events_outbox",
        "run_events",
        ["published", "run_id", "sequence"],
    )

    # ---- 2. tamper-evident audit: columns on audit_log ---------------------
    op.add_column("audit_log", sa.Column("seq", sa.Integer(), nullable=True))
    op.add_column("audit_log", sa.Column("prev_hash", sa.String(64), nullable=True))
    op.add_column("audit_log", sa.Column("entry_hash", sa.String(64), nullable=True))
    # UNIQUE INDEX (not a table constraint): addable by ALTER on SQLite. NULL
    # seq rows stay distinct on both dialects, so legacy/system rows are free.
    op.create_index(
        "uq_audit_tenant_seq", "audit_log", ["tenant_id", "seq"], unique=True
    )

    # ---- 3. governance: columns on workflows + workflow_versions -----------
    op.add_column(
        "workflows",
        sa.Column(
            "requires_approval", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
    )
    op.add_column(
        "workflows",
        sa.Column("sensitivity", sa.String(32), nullable=False, server_default="normal"),
    )
    op.add_column(
        "workflow_versions",
        sa.Column(
            "requires_approval", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
    )
    op.add_column(
        "workflow_versions",
        sa.Column("sensitivity", sa.String(32), nullable=False, server_default="normal"),
    )

    # ---- new tables --------------------------------------------------------
    op.create_table(
        "run_checkpoints",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.Uuid(),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "run_id",
            sa.Uuid(),
            sa.ForeignKey("runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("layer_index", sa.Integer(), nullable=False),
        sa.Column("completed_node_ids", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("env", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("run_id", "layer_index", name="uq_run_checkpoint_layer"),
    )
    op.create_index("ix_run_checkpoints_tenant_id", "run_checkpoints", ["tenant_id"])
    op.create_index("ix_run_checkpoints_run_id", "run_checkpoints", ["run_id"])

    op.create_table(
        "approval_requests",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.Uuid(),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("subject_type", sa.String(32), nullable=False),
        sa.Column("subject_ref", sa.String(128), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column(
            "requested_by",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "decided_by",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reason", sa.String(2000), nullable=False, server_default=""),
        sa.Column("context", sa.JSON(), nullable=False, server_default="{}"),
    )
    op.create_index(
        "ix_approval_requests_tenant_id", "approval_requests", ["tenant_id"]
    )
    op.create_index(
        "ix_approval_requests_tenant_status",
        "approval_requests",
        ["tenant_id", "status"],
    )
    op.create_index(
        "ix_approval_requests_subject",
        "approval_requests",
        ["tenant_id", "subject_type", "subject_ref"],
    )

    op.create_table(
        "retention_policies",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.Uuid(),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("resource_type", sa.String(64), nullable=False),
        sa.Column("ttl_days", sa.Integer(), nullable=True),
        sa.Column(
            "updated_by",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "tenant_id", "resource_type", name="uq_retention_tenant_resource"
        ),
    )
    op.create_index(
        "ix_retention_policies_tenant_id", "retention_policies", ["tenant_id"]
    )

    op.create_table(
        "stored_objects",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.Uuid(),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "run_id",
            sa.Uuid(),
            sa.ForeignKey("runs.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("uri", sa.String(512), nullable=False),
        sa.Column("key", sa.String(512), nullable=False),
        sa.Column("kind", sa.String(64), nullable=True),
        sa.Column("size", sa.Integer(), nullable=True),
        sa.Column("sha256", sa.String(64), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column(
            "legal_hold", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column("erased_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("tenant_id", "uri", name="uq_stored_object_uri"),
    )
    op.create_index("ix_stored_objects_tenant_id", "stored_objects", ["tenant_id"])
    op.create_index("ix_stored_objects_run_id", "stored_objects", ["run_id"])

    op.create_table(
        "human_tasks",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.Uuid(),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "run_id",
            sa.Uuid(),
            sa.ForeignKey("runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("node_id", sa.String(64), nullable=False),
        sa.Column("prompt", sa.String(8000), nullable=False, server_default=""),
        sa.Column("expects", sa.String(16), nullable=False, server_default="text"),
        sa.Column("assigned_role", sa.String(32), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("escalation_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("escalation", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column(
            "responded_by",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("responded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("response", sa.String(8000), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("run_id", "node_id", name="uq_human_task_run_node"),
    )
    op.create_index("ix_human_tasks_tenant_id", "human_tasks", ["tenant_id"])
    op.create_index(
        "ix_human_tasks_tenant_status", "human_tasks", ["tenant_id", "status"]
    )
    op.create_index(
        "ix_human_tasks_deadline", "human_tasks", ["status", "deadline_at"]
    )

    # ---- RLS for the new tenant-scoped tables (Postgres only) --------------
    if op.get_bind().dialect.name == "postgresql":
        for table in _NEW_TENANT_TABLES:
            _enable_rls(table)


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        for table in _NEW_TENANT_TABLES:
            op.execute(f"DROP POLICY IF EXISTS {table}_tenant_isolation ON {table}")
            op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
            op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")

    op.drop_index("ix_human_tasks_deadline", table_name="human_tasks")
    op.drop_index("ix_human_tasks_tenant_status", table_name="human_tasks")
    op.drop_index("ix_human_tasks_tenant_id", table_name="human_tasks")
    op.drop_table("human_tasks")

    op.drop_index("ix_stored_objects_run_id", table_name="stored_objects")
    op.drop_index("ix_stored_objects_tenant_id", table_name="stored_objects")
    op.drop_table("stored_objects")

    op.drop_index(
        "ix_retention_policies_tenant_id", table_name="retention_policies"
    )
    op.drop_table("retention_policies")

    op.drop_index("ix_approval_requests_subject", table_name="approval_requests")
    op.drop_index(
        "ix_approval_requests_tenant_status", table_name="approval_requests"
    )
    op.drop_index("ix_approval_requests_tenant_id", table_name="approval_requests")
    op.drop_table("approval_requests")

    op.drop_index("ix_run_checkpoints_run_id", table_name="run_checkpoints")
    op.drop_index("ix_run_checkpoints_tenant_id", table_name="run_checkpoints")
    op.drop_table("run_checkpoints")

    op.drop_column("workflow_versions", "sensitivity")
    op.drop_column("workflow_versions", "requires_approval")
    op.drop_column("workflows", "sensitivity")
    op.drop_column("workflows", "requires_approval")

    op.drop_index("uq_audit_tenant_seq", table_name="audit_log")
    op.drop_column("audit_log", "entry_hash")
    op.drop_column("audit_log", "prev_hash")
    op.drop_column("audit_log", "seq")

    op.drop_index("ix_run_events_outbox", table_name="run_events")
    op.drop_column("run_events", "published_at")
    op.drop_column("run_events", "published")

    op.drop_column("runs", "erased_at")
    op.drop_column("runs", "legal_hold")
    op.drop_column("runs", "resume_count")
    op.drop_column("runs", "checkpoint")
    op.drop_column("runs", "mode")
