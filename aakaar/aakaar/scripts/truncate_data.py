"""Reset the dev database to "fresh install" state — preserve only superusers.

Run with:
    cd aakaar && .venv/bin/python -m aakaar.scripts.truncate_data

What it does:
  - Deletes every row in: run_events, runs, chat_messages, chat_sessions,
    workflow_versions, workflows, capability_grants, audit_log, all
    non-superuser users, and tenants.
  - Removes per-tenant directories on disk (vault + object store), since
    tenant ids no longer exist they would otherwise become orphans.
  - Leaves the FAISS vector index in place (it rebuilds on first request).

What it does NOT do:
  - Touch superuser rows (role=superuser, tenant_id NULL).
  - Migrate or alter schema — run alembic for that.
  - Restart the running API. After truncating, restart so it drops any
    in-memory CapabilityIndex caches keyed off the now-gone tenant ids.

Destructive — there is no undo. Don't run against a production DB.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from aakaar.core.config import load_settings
from aakaar.db.models import (
    AuditLog,
    CapabilityGrant,
    ChatMessage,
    ChatSession,
    Run,
    RunEvent,
    Tenant,
    User,
    UserRole,
    Workflow,
    WorkflowVersion,
)
from aakaar.db.session import EngineConfig, SessionFactory, make_engine

# Order matters: delete child rows before parents to satisfy FK constraints
# even when ondelete=CASCADE is missing or partial. Tenants are last so
# the cascades they have are still useful for stragglers.
_DELETE_ORDER = (
    RunEvent,
    Run,
    ChatMessage,
    ChatSession,
    WorkflowVersion,
    Workflow,
    CapabilityGrant,
    AuditLog,
)


def _summarize_users(session: Session) -> tuple[int, int]:
    """Return (total_users, superuser_count)."""
    total = len(session.scalars(select(User.id)).all())
    supers = len(
        session.scalars(
            select(User.id).where(User.role == UserRole.SUPERUSER)
        ).all()
    )
    return total, supers


def _wipe_tenant_dirs(data_dir: Path) -> list[Path]:
    """Remove per-tenant subdirectories under {data}/vault and
    {data}/objects. Returns the list of paths cleared."""
    cleared: list[Path] = []
    for sub in ("vault", "objects"):
        d = data_dir / sub
        if not d.is_dir():
            continue
        for child in d.iterdir():
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
                cleared.append(child)
    return cleared


def main() -> int:
    settings = load_settings()
    engine = make_engine(EngineConfig(url=settings.db_url))
    factory = SessionFactory(engine)

    with factory.session() as s:
        before_total, before_supers = _summarize_users(s)
        before_tenants = len(s.scalars(select(Tenant.id)).all())
        print(
            f"Before: {before_tenants} tenants, {before_total} users "
            f"({before_supers} superusers)"
        )

        if before_supers == 0:
            print(
                "No superuser found — refusing to truncate. Set "
                "AAKAAR_SUPERUSER_EMAIL/PASSWORD and start the API once "
                "to bootstrap one before running this script.",
                file=sys.stderr,
            )
            return 2

        for model in _DELETE_ORDER:
            s.execute(delete(model))
        # Drop every non-superuser user, then every tenant.
        s.execute(delete(User).where(User.role != UserRole.SUPERUSER))
        s.execute(delete(Tenant))
        s.commit()

        after_total, after_supers = _summarize_users(s)
        after_tenants = len(s.scalars(select(Tenant.id)).all())
        print(
            f"After:  {after_tenants} tenants, {after_total} users "
            f"({after_supers} superusers preserved)"
        )

    cleared = _wipe_tenant_dirs(Path(settings.data_dir))
    if cleared:
        print(f"Cleared {len(cleared)} per-tenant directories on disk.")
    print("Done. Restart the API so it drops cached CapabilityIndex state.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
