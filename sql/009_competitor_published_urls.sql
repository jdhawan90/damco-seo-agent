-- ============================================================================
-- Damco SEO AI Agent System — competitor_published_urls (migration 009)
--
-- A manifest of every URL we've ever discovered in a tracked competitor's
-- sitemap. The competitive_intelligence.content_monitor diffs the current
-- sitemap against this manifest to detect "competitor just published a
-- new page" events — the signal we want at low SERP latency, before the
-- URL even has a chance to rank for one of our keywords.
--
-- Distinct from `competitor_pages`:
--   - competitor_pages stores the current crawled state of a URL we're
--     actively monitoring (title, meta, content hash, etc.)
--   - competitor_published_urls is just the existence manifest —
--     "we know this URL exists on the sitemap as of last_seen"
--
-- Idempotent.
-- ============================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS competitor_published_urls (
    id              BIGSERIAL PRIMARY KEY,
    competitor_id   BIGINT       NOT NULL REFERENCES competitors(id) ON DELETE CASCADE,
    url             TEXT         NOT NULL,
    sitemap_source  TEXT,        -- which sitemap entry-point URL surfaced this row
    first_seen      DATE         NOT NULL DEFAULT CURRENT_DATE,
    last_seen       DATE         NOT NULL DEFAULT CURRENT_DATE,
    is_active       BOOLEAN      NOT NULL DEFAULT TRUE,  -- false when URL drops out of sitemap
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
    UNIQUE (competitor_id, url)
);

CREATE INDEX IF NOT EXISTS idx_cpub_urls_competitor   ON competitor_published_urls (competitor_id);
CREATE INDEX IF NOT EXISTS idx_cpub_urls_first_seen   ON competitor_published_urls (first_seen DESC);
CREATE INDEX IF NOT EXISTS idx_cpub_urls_is_active    ON competitor_published_urls (competitor_id, is_active);

COMMENT ON TABLE competitor_published_urls IS
    'Manifest of URLs ever observed in tracked competitor sitemaps. Populated by competitive_intelligence.content_monitor.';

COMMIT;
