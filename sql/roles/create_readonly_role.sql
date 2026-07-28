-- ============================================================================
-- Read-only role for the dashboard chat — SUPERUSER STEP
--
-- NOT a migration. It lives in sql/roles/ deliberately: sql/migrate.py globs
-- `sql/*.sql` (flat), so this file is never picked up and run by the app user,
-- which could not create a role anyway.
--
-- Why a separate role at all
-- --------------------------
-- The dashboard chat can fall back to running model-authored SQL. That query
-- text is not reviewed by a human before it executes. Running it as the
-- application user — which can INSERT, UPDATE, DELETE and DROP — would mean a
-- prompt injection in a competitor's page title, an RSS item, or a SERP
-- snippet is one bad generation away from mutating production data.
--
-- This role can only SELECT. That is the guardrail; the statement timeout and
-- row cap in dashboard/chat.py are the second and third.
--
-- Run as a superuser
-- ------------------
--   psql -U postgres -d damco_seo \
--        -v pw="'a-strong-password-here'" \
--        -f sql/roles/create_readonly_role.sql
--
-- Then put the connection string in .env as DATABASE_URL_READONLY. Do not
-- commit it — .env is gitignored.
--
-- After running this, apply the grants migration as normal:
--   python sql/migrate.py
--
-- Idempotent.
-- ============================================================================

\set ON_ERROR_STOP on

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'seo_readonly') THEN
        EXECUTE format('CREATE ROLE seo_readonly LOGIN PASSWORD %L', :'pw');
        RAISE NOTICE 'Created role seo_readonly.';
    ELSE
        EXECUTE format('ALTER ROLE seo_readonly WITH LOGIN PASSWORD %L', :'pw');
        RAISE NOTICE 'Role seo_readonly already existed — password updated.';
    END IF;
END
$$;

-- Belt and braces. The role should never be able to write even if someone
-- later grants it a table by accident.
ALTER ROLE seo_readonly SET default_transaction_read_only = on;

-- A model-authored query should never be able to pin a connection open.
ALTER ROLE seo_readonly SET statement_timeout = '5s';
ALTER ROLE seo_readonly SET idle_in_transaction_session_timeout = '10s';

-- Keep it out of anything that isn't the application schema.
REVOKE ALL ON SCHEMA public FROM seo_readonly;
GRANT CONNECT ON DATABASE damco_seo TO seo_readonly;
GRANT USAGE ON SCHEMA public TO seo_readonly;

RAISE NOTICE 'Now run: python sql/migrate.py   (applies the SELECT grants)';
