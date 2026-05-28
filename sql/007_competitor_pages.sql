-- ============================================================================
-- Damco SEO AI Agent System — competitor_pages (migration 007)
--
-- Stores the current crawled state of each tracked competitor URL so the
-- competitor_monitor can diff against it on the next crawl and emit
-- competitor_changes events.
--
-- Mirror structure of `pages` table fields that site_auditor populates
-- for our own URLs, so the same downstream queries work across both.
--
-- Idempotent.
-- ============================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS competitor_pages (
    id                BIGSERIAL PRIMARY KEY,
    competitor_id     BIGINT       NOT NULL REFERENCES competitors(id) ON DELETE CASCADE,
    url               TEXT         NOT NULL,
    title             TEXT,
    meta_description  TEXT,
    h1                TEXT,             -- first h1 only (most pages have one)
    canonical_url     TEXT,
    lang              TEXT,
    word_count        INTEGER,
    schema_types      JSONB        NOT NULL DEFAULT '[]'::jsonb,  -- array of distinct @type values
    has_microdata     BOOLEAN      NOT NULL DEFAULT FALSE,
    content_hash      TEXT,             -- sha256 of normalized visible text
    last_status       INTEGER,          -- HTTP status from last fetch
    last_fetched_at   TIMESTAMPTZ,
    created_at        TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ  NOT NULL DEFAULT now(),
    UNIQUE (competitor_id, url)
);

CREATE INDEX IF NOT EXISTS idx_competitor_pages_competitor  ON competitor_pages (competitor_id);
CREATE INDEX IF NOT EXISTS idx_competitor_pages_last_fetched ON competitor_pages (last_fetched_at DESC NULLS LAST);
CREATE INDEX IF NOT EXISTS idx_competitor_pages_content_hash ON competitor_pages (content_hash);

-- Reuse the updated_at trigger pattern from migration 001
DO $$
BEGIN
    EXECUTE 'DROP TRIGGER IF EXISTS trg_competitor_pages_updated_at ON competitor_pages';
    EXECUTE 'CREATE TRIGGER trg_competitor_pages_updated_at
             BEFORE UPDATE ON competitor_pages
             FOR EACH ROW EXECUTE FUNCTION set_updated_at()';
END $$;

COMMENT ON TABLE competitor_pages IS
    'Current snapshot of crawled state per competitor URL. competitor_monitor diffs against this on each run.';

COMMIT;
