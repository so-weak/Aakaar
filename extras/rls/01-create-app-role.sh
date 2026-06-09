#!/bin/sh
# Postgres init hook (runs ONCE, on a fresh data volume, as the superuser).
# Creates the non-superuser `aakaar_app` role the API connects as, so that
# FORCE ROW LEVEL SECURITY (migration 0007) actually enforces tenant isolation.
# Mounted into /docker-entrypoint-initdb.d/ by docker-compose.
set -e

APP_PASSWORD="${AAKAAR_APP_DB_PASSWORD:-aakaar_app}"

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<SQL
DO \$\$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'aakaar_app') THEN
    EXECUTE format(
      'CREATE ROLE aakaar_app LOGIN PASSWORD %L NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS',
      '${APP_PASSWORD}'
    );
  END IF;
END
\$\$;

-- aakaar_app runs the migrations at boot, so let it CREATE (and own) objects.
-- Owning the tables + FORCE ROW LEVEL SECURITY = RLS enforced for it.
GRANT USAGE, CREATE ON SCHEMA public TO aakaar_app;
SQL

echo "aakaar: created non-superuser role 'aakaar_app' for RLS-enforced runtime"
