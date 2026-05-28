-- ============================================================================
-- Damco SEO AI Agent System — competitor_backlinks (migration 008)
--
-- Storage for backlinks pointing at competitor domains. Separate from
-- the existing `backlinks` table because:
--   - competitor backlinks have different ownership (competitor_id FK
--     instead of page_id) and different lifecycle (refreshed monthly,
--     not when our pages change)
--   - cross-analysis joins are cleaner with a dedicated table
--   - keeping them separate prevents accidental mixing of "our links"
--     and "their links" in queries
--
-- Idempotent.
-- ============================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS competitor_backlinks (
    id                BIGSERIAL PRIMARY KEY,
    competitor_id     BIGINT       NOT NULL REFERENCES competitors(id) ON DELETE CASCADE,
    source_url        TEXT         NOT NULL,
    source_domain     TEXT         NOT NULL,
    target_url        TEXT,                              -- specific competitor page being linked
    anchor_text       TEXT,
    is_dofollow       BOOLEAN,
    domain_rank       INTEGER                            -- DataForSEO authority (0-100)
                      CHECK (domain_rank IS NULL OR domain_rank BETWEEN 0 AND 100),
    first_seen        DATE,                              -- earliest DataForSEO saw this link
    last_seen         DATE,                              -- most recent confirmation
    date_discovered   DATE         NOT NULL DEFAULT CURRENT_DATE,
    data_source       TEXT         NOT NULL DEFAULT 'dataforseo',
    raw_payload       JSONB        NOT NULL DEFAULT '{}'::jsonb,
    created_at        TIMESTAMPTZ  NOT NULL DEFAULT now(),
    UNIQUE (competitor_id, source_url, data_source)
);

CREATE INDEX IF NOT EXISTS idx_competitor_backlinks_competitor    ON competitor_backlinks (competitor_id);
CREATE INDEX IF NOT EXISTS idx_competitor_backlinks_source_domain ON competitor_backlinks (source_domain);
CREATE INDEX IF NOT EXISTS idx_competitor_backlinks_domain_rank   ON competitor_backlinks (domain_rank DESC NULLS LAST);
CREATE INDEX IF NOT EXISTS idx_competitor_backlinks_discovered    ON competitor_backlinks (date_discovered DESC);

COMMENT ON TABLE competitor_backlinks IS
    'Backlinks pointing at tracked competitor domains. Populated by competitive_intelligence.backlink_analyzer via DataForSEO Backlinks API.';

COMMIT;
