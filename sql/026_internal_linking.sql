-- ============================================================================
-- Internal linking: extend existing internal_links + new suggestions table
--                                                                (migration 026)
--
-- The `internal_links` table was created in migration 001 with (source_url,
-- target_url, anchor_text, date_crawled). 15,918 existing rows hold crawl
-- output from an earlier technical_seo module, so we EXTEND the table rather
-- than recreating it: new columns capture the signals the detectors need
-- (rel, is_internal, first_seen/last_seen semantics).
--
-- Idempotent — every DDL uses IF NOT EXISTS / IF EXISTS.
-- ============================================================================

BEGIN;

-- ---------------------------------------------------------------------------
-- 1. internal_links  — extend with crawl-signal columns
-- ---------------------------------------------------------------------------
ALTER TABLE internal_links
    ADD COLUMN IF NOT EXISTS rel         TEXT,
    ADD COLUMN IF NOT EXISTS is_internal BOOLEAN NOT NULL DEFAULT TRUE,
    ADD COLUMN IF NOT EXISTS first_seen  DATE,
    ADD COLUMN IF NOT EXISTS last_seen   DATE;

-- Backfill: for existing rows seed first_seen / last_seen from date_crawled
-- so they aren't NULL and the detectors' freshness check works immediately.
UPDATE internal_links
   SET first_seen = coalesce(first_seen, date_crawled::date),
       last_seen  = coalesce(last_seen,  date_crawled::date)
 WHERE first_seen IS NULL OR last_seen IS NULL;

CREATE INDEX IF NOT EXISTS idx_internal_links_last_seen
    ON internal_links (last_seen);
CREATE INDEX IF NOT EXISTS idx_internal_links_is_internal
    ON internal_links (is_internal);


-- ---------------------------------------------------------------------------
-- 2. internal_link_suggestions  — detector output
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS internal_link_suggestions (
    id                      BIGSERIAL PRIMARY KEY,
    detector                TEXT NOT NULL,
    from_url                TEXT NOT NULL,
    to_url                  TEXT NOT NULL,
    suggested_anchor        TEXT,
    reason                  TEXT NOT NULL,
    priority_score          INT NOT NULL DEFAULT 0,
    status                  TEXT NOT NULL DEFAULT 'new'
                             CHECK (status IN ('new', 'applied', 'ignored', 'stale')),
    first_suggested_at      TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_suggested_at       TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    applied_at              TIMESTAMPTZ,
    notes                   TEXT,
    UNIQUE (detector, from_url, to_url)
);

CREATE INDEX IF NOT EXISTS idx_link_sugg_detector_status
    ON internal_link_suggestions (detector, status);
CREATE INDEX IF NOT EXISTS idx_link_sugg_to_url
    ON internal_link_suggestions (to_url);
CREATE INDEX IF NOT EXISTS idx_link_sugg_priority
    ON internal_link_suggestions (priority_score DESC);

COMMIT;
