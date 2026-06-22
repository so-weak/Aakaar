#!/usr/bin/env bash
# Rename a user's login email in the local SQLite DB.
#
# Why: the API's login schema validates email with the regex
#   ^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$   (aakaar/api/schemas.py)
# which REQUIRES a dotted domain. "soubhik@super" has no TLD, so login is
# rejected with HTTP 422 before any password check. Renaming it to a valid
# address fixes that. The user id, role, password and everything else are
# unchanged — only the `email` (the login identifier) changes.
#
# Defaults (exactly what was asked):
#   soubhik@super  ->  soubhik@super.test
#
# Override via env vars:
#   OLD_EMAIL='...'                current email          [soubhik@super]
#   NEW_EMAIL='...'                new email              [soubhik@super.test]
#   AAKAAR_DB_FILE=/path/to.sqlite database file          [aakaar/data/aakaar.sqlite]
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${AAKAAR_PYTHON:-python3}"
DB="${AAKAAR_DB_FILE:-$ROOT/aakaar/data/aakaar.sqlite}"
OLD_EMAIL="${OLD_EMAIL:-soubhik@super}"
NEW_EMAIL="${NEW_EMAIL:-soubhik@super.test}"

if ! command -v "$PY" >/dev/null 2>&1; then
  echo "error: '$PY' not found on PATH (set AAKAAR_PYTHON)." >&2
  exit 1
fi
if [ ! -f "$DB" ]; then
  echo "error: database not found at $DB (set AAKAAR_DB_FILE to override)." >&2
  exit 1
fi

echo "Renaming user email:"
echo "  db:   $DB"
echo "  from: $OLD_EMAIL"
echo "  to:   $NEW_EMAIL"
echo

# Pure SQL rename (no app code needed). Uses Python's stdlib sqlite3 so the
# server venv isn't required. Validates the new address against the SAME regex
# the API enforces, refuses to clobber an existing email, and only commits if
# exactly one row changed.
OLD_EMAIL="$OLD_EMAIL" NEW_EMAIL="$NEW_EMAIL" DB="$DB" "$PY" - <<'PY'
import os
import re
import sqlite3
import sys

db = os.environ["DB"]
old = os.environ["OLD_EMAIL"]
new = os.environ["NEW_EMAIL"]

# Same pattern as aakaar/api/schemas.py EmailStr — the new value must be a valid
# login email or the rename wouldn't fix anything.
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
if not EMAIL_RE.match(new):
    print(f"error: NEW_EMAIL '{new}' is not a valid login email (would still be rejected).", file=sys.stderr)
    sys.exit(2)

conn = sqlite3.connect(db)
try:
    src = conn.execute(
        "SELECT id, role, tenant_id FROM users WHERE email = ?", (old,)
    ).fetchall()
    if not src:
        print(f"error: no user found with email '{old}' — nothing to do.", file=sys.stderr)
        sys.exit(1)
    if len(src) > 1:
        print(f"error: {len(src)} users share email '{old}'; refusing to rename ambiguously.", file=sys.stderr)
        sys.exit(1)

    uid, role, tenant_id = src[0]

    # Don't create a duplicate login within the same tenant scope (the
    # uq_users_tenant_email constraint is per (tenant_id, email)).
    if tenant_id is None:
        clash = conn.execute(
            "SELECT id FROM users WHERE email = ? AND tenant_id IS NULL AND id != ?",
            (new, uid),
        ).fetchone()
    else:
        clash = conn.execute(
            "SELECT id FROM users WHERE email = ? AND tenant_id = ? AND id != ?",
            (new, tenant_id, uid),
        ).fetchone()
    if clash is not None:
        print(f"error: email '{new}' is already taken in this tenant — aborting.", file=sys.stderr)
        sys.exit(1)

    cur = conn.execute(
        "UPDATE users SET email = ? WHERE id = ?", (new, uid)
    )
    if cur.rowcount != 1:
        print(f"error: expected to update 1 row, updated {cur.rowcount}; rolling back.", file=sys.stderr)
        conn.rollback()
        sys.exit(1)

    conn.commit()
    print(f"  ✓ {old} -> {new}")
    print(f"    id={uid} role={role} tenant_id={tenant_id or 'NULL (superuser)'}")
finally:
    conn.close()
PY

echo
echo "Done. Log in with the new email: $NEW_EMAIL"
