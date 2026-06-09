"""users: MFA (TOTP) + OIDC federation columns

Adds the per-user state for two new auth factors:
  * MFA/TOTP  — mfa_enabled, totp_secret, totp_pending_secret, totp_last_step,
                mfa_recovery_codes
  * OIDC/SSO  — oidc_subject (unique-when-present), last_login_at

All columns are additive and nullable (or carry a server default), so the
migration is safe on a populated table. Existing password users are unaffected:
mfa_enabled defaults to false and oidc_subject stays NULL.

Revision ID: 0006
Revises: 0005
Create Date: 2026-06-10
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "mfa_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column("users", sa.Column("totp_secret", sa.String(256), nullable=True))
    op.add_column("users", sa.Column("totp_pending_secret", sa.String(256), nullable=True))
    op.add_column("users", sa.Column("totp_last_step", sa.Integer(), nullable=True))
    op.add_column("users", sa.Column("mfa_recovery_codes", sa.JSON(), nullable=True))
    op.add_column("users", sa.Column("oidc_subject", sa.String(320), nullable=True))
    op.add_column(
        "users",
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
    )
    # Unique when present; NULLs are distinct on both SQLite and Postgres, so
    # local/password users (oidc_subject NULL) are unconstrained.
    op.create_index(
        "uq_users_oidc_subject", "users", ["oidc_subject"], unique=True
    )


def downgrade() -> None:
    op.drop_index("uq_users_oidc_subject", table_name="users")
    op.drop_column("users", "last_login_at")
    op.drop_column("users", "oidc_subject")
    op.drop_column("users", "mfa_recovery_codes")
    op.drop_column("users", "totp_last_step")
    op.drop_column("users", "totp_pending_secret")
    op.drop_column("users", "totp_secret")
    op.drop_column("users", "mfa_enabled")
