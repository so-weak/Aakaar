"""remote_agents

Revision ID: 0004
Revises: 0003
Create Date: 2026-06-04
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "remote_agents",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.Uuid(),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("alias", sa.String(64), nullable=False),
        sa.Column("api_key_hash", sa.String(255), nullable=False),
        sa.Column("os", sa.String(32), nullable=True),
        sa.Column("hostname", sa.String(255), nullable=True),
        sa.Column("gui_capable", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("pools", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("capabilities", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("agent_version", sa.String(32), nullable=True),
        sa.Column("status", sa.String(32), nullable=False, server_default="enrolled"),
        sa.Column("last_seen", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_by",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("tenant_id", "alias", name="uq_remote_agent_tenant_alias"),
    )
    op.create_index("ix_remote_agents_tenant_id", "remote_agents", ["tenant_id"])


def downgrade() -> None:
    op.drop_index("ix_remote_agents_tenant_id", table_name="remote_agents")
    op.drop_table("remote_agents")
