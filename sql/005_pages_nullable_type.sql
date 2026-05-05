-- ============================================================================
-- Damco SEO AI Agent System -- relax pages.page_type (migration 005)
--
-- Auto-discovery from sitemaps surfaces URLs that don't cleanly map to any of
-- the existing page_type values (e.g. /about-us, /contact-us). Defaulting
-- those to 'blog' (the previous default) was actively misleading -- it
-- pretended we knew the category.
--
-- Change: allow page_type to be NULL. CHECK constraint stays (NULL passes
-- CHECK by definition, set values are still validated). Drop the default.
--
-- Idempotent.
-- ============================================================================

BEGIN;

ALTER TABLE pages ALTER COLUMN page_type DROP NOT NULL;
ALTER TABLE pages ALTER COLUMN page_type DROP DEFAULT;

COMMENT ON COLUMN pages.page_type IS
    'NULL when auto-discovery could not determine the type. Curators set this manually for ambiguous pages (about-us, contact, etc.).';

COMMIT;
