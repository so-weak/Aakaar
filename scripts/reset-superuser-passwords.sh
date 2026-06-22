#!/usr/bin/env bash
# Reset superuser password(s) in the local SQLite DB.
#
# Passwords are stored as SHA-256 -> bcrypt hashes (aakaar/api/auth/passwords.py)
# and are NOT recoverable, so this OVERWRITES them with a known value using the
# app's own hasher (raw bcrypt would not verify). Dev / account-recovery helper.
#
# Defaults (exactly what was asked): set BOTH superusers
#   - soubhik@super
#   - pracharya@aakar.test
# to the password "soubhik@super".
#
# Override via env vars:
#   NEW_PASSWORD='...'              password to set        [soubhik@super]
#   TARGET_EMAILS='a@b c@d'         space-separated emails [the two superusers]
#   AAKAAR_DB_FILE=/path/to.sqlite  database file          [aakaar/data/aakaar.sqlite]
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$ROOT/aakaar/.venv/bin/python"
DB="${AAKAAR_DB_FILE:-$ROOT/aakaar/data/aakaar.sqlite}"
NEW_PASSWORD="${NEW_PASSWORD:-soubhik@super}"
TARGET_EMAILS="${TARGET_EMAILS:-soubhik@super pracharya@aakar.test}"

if [ ! -x "$PY" ]; then
  echo "error: server venv not found at $PY" >&2
  echo "       run scripts/start-server.sh once to create it, or set AAKAAR_PYTHON." >&2
  exit 1
fi
if [ ! -f "$DB" ]; then
  echo "error: database not found at $DB (set AAKAAR_DB_FILE to override)." >&2
  exit 1
fi

echo "Resetting superuser password(s):"
echo "  db:       $DB"
echo "  emails:   $TARGET_EMAILS"
echo "  password: $NEW_PASSWORD"
echo

# The hash + UPDATE happen in the server venv so we can reuse the app's exact
# hashing (and verify the new hash validates before committing). cd into the
# package dir so the editable 'aakaar' import resolves regardless of caller CWD.
cd "$ROOT/aakaar"
NEW_PASSWORD="$NEW_PASSWORD" TARGET_EMAILS="$TARGET_EMAILS" DB="$DB" "$PY" - <<'PY'
import os
import sqlite3
import sys

from aakaar.api.auth.passwords import hash_password, verify_password

db = os.environ["DB"]
pw = os.environ["NEW_PASSWORD"]
emails = os.environ["TARGET_EMAILS"].split()

conn = sqlite3.connect(db)
try:
    changed = 0
    for email in emails:
        # Fresh salt per user, so identical passwords still get distinct hashes.
        new_hash = hash_password(pw)
        cur = conn.execute(
            "UPDATE users SET password_hash = ? WHERE email = ?", (new_hash, email)
        )
        if cur.rowcount == 0:
            print(f"  ! no user found: {email}", file=sys.stderr)
            continue
        # Read back and confirm the stored hash verifies against the new password.
        row = conn.execute(
            "SELECT password_hash FROM users WHERE email = ?", (email,)
        ).fetchone()
        ok = verify_password(pw, row[0])
        if not ok:
            print(f"  x {email}: hash did NOT verify — aborting, no changes saved", file=sys.stderr)
            conn.rollback()
            sys.exit(2)
        print(f"  ✓ {email}: password reset")
        changed += 1
    if changed == 0:
        print("no matching users — nothing changed", file=sys.stderr)
        sys.exit(1)
    conn.commit()
    print(f"\ncommitted: {changed} user(s) updated")
finally:
    conn.close()
PY

echo
echo "Done. Log in with:"
echo "  password: $NEW_PASSWORD"
