-- ============================================================================
-- SELECT grants for the dashboard chat role (migration 017)
--
-- Pairs with sql/roles/create_readonly_role.sql, which is the superuser step
-- and is deliberately not a migration.
--
-- This file is safe to apply as the ordinary application user. If the role
-- does not exist yet it does nothing but print a notice — it will not fail the
-- migration run, because the read-only role is only needed by the dashboard
-- chat and shouldn't block anyone setting up the agents.
--
-- Idempotent.
-- ============================================================================

BEGIN;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'seo_readonly') THEN
        RAISE NOTICE
            'Role seo_readonly does not exist — skipping grants. The dashboard '
            'chat''s SQL fallback will be unavailable until you run '
            'sql/roles/create_readonly_role.sql as a superuser and re-run this '
            'migration.';
        RETURN;
    END IF;

    GRANT USAGE ON SCHEMA public TO seo_readonly;
    GRANT SELECT ON ALL TABLES IN SCHEMA public TO seo_readonly;
    GRANT SELECT ON ALL SEQUENCES IN SCHEMA public TO seo_readonly;

    -- Future tables too. Without this, every new migration silently creates a
    -- table the chat cannot read, and the failure shows up as a confusing
    -- "permission denied" months later.
    ALTER DEFAULT PRIVILEGES IN SCHEMA public
        GRANT SELECT ON TABLES TO seo_readonly;
    ALTER DEFAULT PRIVILEGES IN SCHEMA public
        GRANT SELECT ON SEQUENCES TO seo_readonly;

    RAISE NOTICE 'Granted SELECT on all current and future tables to seo_readonly.';
END
$$;

COMMIT;
