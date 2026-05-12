-- ============================================================================
-- Damco SEO AI Agent System -- persist audit-time page metadata (migration 006)
--
-- Until now, technical_seo.site_auditor only emits issues; it doesn't persist
-- the page-level audit data the crawler captures (title, meta description,
-- canonical, lang). Storing these on `pages` enables cross-page checks the
-- per-page detectors can't do alone — e.g. duplicate-title detection, which
-- needs to GROUP BY title across the site.
--
-- The `title` and `word_count` columns already exist (migration 001) but are
-- unused. This migration adds the missing ones.
--
-- Idempotent.
-- ============================================================================

BEGIN;

ALTER TABLE pages
    ADD COLUMN IF NOT EXISTS meta_description TEXT,
    ADD COLUMN IF NOT EXISTS canonical_url    TEXT,
    ADD COLUMN IF NOT EXISTS lang             TEXT;

COMMENT ON COLUMN pages.title            IS 'Page <title> as of last_audited. Populated by site_auditor.';
COMMENT ON COLUMN pages.meta_description IS 'Page meta description as of last_audited. Populated by site_auditor.';
COMMENT ON COLUMN pages.canonical_url    IS 'Absolute canonical URL as of last_audited. Populated by site_auditor.';
COMMENT ON COLUMN pages.lang             IS 'html[lang] value as of last_audited. Populated by site_auditor.';
COMMENT ON COLUMN pages.word_count       IS 'Visible-text word count as of last_audited. Populated by site_auditor.';

-- Indexes for the duplicate-content detectors that consume these columns.
CREATE INDEX IF NOT EXISTS idx_pages_title_lower
    ON pages (lower(title)) WHERE title IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_pages_meta_description_lower
    ON pages (lower(meta_description)) WHERE meta_description IS NOT NULL;

COMMIT;
