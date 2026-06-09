-- Aakaar Row-Level Security: dedicated application role
-- ----------------------------------------------------------------------------
-- Postgres RLS is BYPASSED by SUPERUSER / BYPASSRLS roles unconditionally, and
-- by a table's OWNER unless FORCE ROW LEVEL SECURITY is set (migration 0007
-- sets it on every policied table). The default `postgres`-image role is a
-- SUPERUSER, so RLS does nothing while the app connects as it.
--
-- The simplest correct fix needs NO privilege separation: run the app as a
-- single dedicated **non-superuser** role that OWNS its tables. Being a
-- non-superuser, FORCE ROW LEVEL SECURITY applies the policies to it too. It
-- runs the migrations (so it owns every table it creates) and serves runtime
-- traffic on the same connection — one role, one URL.
--
-- First-time setup. Run as the DB superuser/owner against the app database:
--   psql "$ADMIN_DB_URL" -v app_password="'a-strong-password'" -f setup_app_role.sql
-- Then point AAKAAR_DB_URL at:
--   postgresql+psycopg://aakaar_app:<password>@<host>:5432/<db>
-- (The Aakaar container runs `alembic upgrade head` at boot, creating + owning
--  the schema as this role. In docker-compose this is automatic — see
--  extras/rls/01-create-app-role.sh.)
-- ----------------------------------------------------------------------------

CREATE ROLE aakaar_app LOGIN PASSWORD :'app_password'
  NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;

-- Let aakaar_app use and CREATE objects in the schema, so its boot-time
-- migrations own every table — which is what makes FORCE RLS bind to it.
GRANT USAGE, CREATE ON SCHEMA public TO aakaar_app;

-- Verify (rows are scoped by the transaction-local app.tenant_id GUC):
--   SET ROLE aakaar_app;
--   SELECT set_config('app.tenant_id', '<tenant-uuid>', false);  SELECT count(*) FROM runs;  -- that tenant only
--   SELECT set_config('app.tenant_id', 'system', false);         SELECT count(*) FROM runs;  -- all
--   SELECT set_config('app.tenant_id', '', false);               SELECT count(*) FROM runs;  -- 0 (fail-closed)
--   RESET ROLE;
