-- ============================================================================
-- Pages backfiller: per-decision audit table                   (migration 027)
--
-- The backfiller runs in two phases:
--   1. hydrate  — crawl pages with NULL title / meta_description and fill them
--                 (no schema change needed; it writes existing columns)
--   2. classify — infer `pages.offering` for rows where it is NULL by scoring
--                 the hydrated URL + title + meta against tenant offering
--                 vocabulary.
--
-- `offering_backfill_log` records one row per classification attempt so the
-- SEO team can audit why each page landed where it did. Without this, the
-- first time a surprising assignment shows up it would be impossible to
-- reproduce the decision — vocabulary drifts, offerings get added, token
-- lists are tuned. The log makes those decisions discoverable after the fact.
--
-- Idempotent.
-- ============================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS offering_backfill_log (
    id                 BIGSERIAL PRIMARY KEY,
    page_id            BIGINT NOT NULL REFERENCES pages(id) ON DELETE CASCADE,
    run_date           TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    -- Final decision for this run. 'assigned' means we wrote pages.offering;
    -- 'ambiguous' / 'no_match' explain why we did not. 'skipped_has_value'
    -- records a page that already had an offering (never overwritten).
    decision           TEXT NOT NULL
                        CHECK (decision IN (
                            'assigned', 'ambiguous', 'no_match',
                            'skipped_has_value', 'dry_run'
                        )),
    assigned_offering  TEXT,                                -- NULL unless decision='assigned'
    confidence_score   NUMERIC(5,2),                        -- 0.00 .. 100.00
    top_offering       TEXT,                                -- winning offering, even if not assigned
    top_score          INT,                                 -- winning offering's score
    runner_up_offering TEXT,
    runner_up_score    INT,
    url_matches        JSONB,                               -- {offering: [tokens matched in url]}
    title_matches      JSONB,                               -- {offering: [tokens matched in title]}
    meta_matches       JSONB                                -- {offering: [tokens matched in meta]}
);

CREATE INDEX IF NOT EXISTS idx_offering_backfill_log_page
    ON offering_backfill_log (page_id, run_date DESC);
CREATE INDEX IF NOT EXISTS idx_offering_backfill_log_decision
    ON offering_backfill_log (decision);
CREATE INDEX IF NOT EXISTS idx_offering_backfill_log_run_date
    ON offering_backfill_log (run_date DESC);

COMMIT;
