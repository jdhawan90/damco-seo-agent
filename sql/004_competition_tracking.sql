-- ============================================================================
-- Damco SEO AI Agent System -- competition tracking schema (migration 004)
--
-- Implements the design at sql/DESIGN_competition_tracking.md. Decisions:
--   * Track top 10 only
--   * Desktop only (default)
--   * Fortnightly snapshot cadence (per-keyword override supported)
--   * Drop competitors.offering (competitors span offerings via join)
--   * Capture AI Overview citations
--   * Refresh materialized view CONCURRENTLY (non-blocking)
--   * Keep all snapshots forever (no retention cap)
--
-- This migration is idempotent. Safe to re-run.
-- ============================================================================

BEGIN;

-- ---------------------------------------------------------------------------
-- 1. keywords: per-keyword snapshot cadence override
-- ---------------------------------------------------------------------------

ALTER TABLE keywords
    ADD COLUMN IF NOT EXISTS snapshot_frequency_days INTEGER NOT NULL DEFAULT 14
        CHECK (snapshot_frequency_days BETWEEN 1 AND 90);

COMMENT ON COLUMN keywords.snapshot_frequency_days IS
    'Days between rank-tracker snapshots. Default 14 (fortnightly). Allows daily for high-priority keywords.';

-- ---------------------------------------------------------------------------
-- 2. competitors: master registry enrichment
-- ---------------------------------------------------------------------------

-- Drop the offering column (competitors span offerings via competitor_rankings join).
-- Safe: table has 0 rows at migration time.
ALTER TABLE competitors DROP COLUMN IF EXISTS offering;

ALTER TABLE competitors
    ADD COLUMN IF NOT EXISTS company_name              TEXT,
    ADD COLUMN IF NOT EXISTS category                  TEXT
        CHECK (category IN ('direct', 'adjacent', 'aggregator', 'informational', 'big_tech', 'internal')),
    ADD COLUMN IF NOT EXISTS threat_tier               TEXT NOT NULL DEFAULT 'peripheral'
        CHECK (threat_tier IN ('primary', 'watch', 'peripheral', 'ignore')),
    ADD COLUMN IF NOT EXISTS domain_authority          INTEGER
        CHECK (domain_authority IS NULL OR domain_authority BETWEEN 0 AND 100),
    ADD COLUMN IF NOT EXISTS country                   TEXT
        CHECK (country IS NULL OR length(country) = 2),
    ADD COLUMN IF NOT EXISTS first_seen_date           DATE,
    ADD COLUMN IF NOT EXISTS last_seen_date            DATE,
    ADD COLUMN IF NOT EXISTS keyword_appearance_count  INTEGER NOT NULL DEFAULT 0
        CHECK (keyword_appearance_count >= 0),
    ADD COLUMN IF NOT EXISTS offering_appearance_count INTEGER NOT NULL DEFAULT 0
        CHECK (offering_appearance_count >= 0),
    ADD COLUMN IF NOT EXISTS is_tracked                BOOLEAN NOT NULL DEFAULT TRUE,
    ADD COLUMN IF NOT EXISTS metadata                  JSONB NOT NULL DEFAULT '{}'::jsonb;

CREATE INDEX IF NOT EXISTS idx_competitors_threat_tier  ON competitors (threat_tier)        WHERE is_tracked = TRUE;
CREATE INDEX IF NOT EXISTS idx_competitors_category     ON competitors (category)           WHERE is_tracked = TRUE;
CREATE INDEX IF NOT EXISTS idx_competitors_last_seen    ON competitors (last_seen_date DESC) WHERE is_tracked = TRUE;
CREATE INDEX IF NOT EXISTS idx_competitors_appearances  ON competitors (keyword_appearance_count DESC, offering_appearance_count DESC)
    WHERE is_tracked = TRUE;

-- ---------------------------------------------------------------------------
-- 3. competitor_rankings: enrich per-position snapshot rows
-- ---------------------------------------------------------------------------

ALTER TABLE competitor_rankings
    ADD COLUMN IF NOT EXISTS url_title           TEXT,
    ADD COLUMN IF NOT EXISTS page_type           TEXT
        CHECK (page_type IN ('service', 'listicle', 'blog', 'docs', 'product', 'homepage', 'unknown')),
    ADD COLUMN IF NOT EXISTS serp_features_owned JSONB NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN IF NOT EXISTS is_new_entrant      BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS previous_position   INTEGER,
    ADD COLUMN IF NOT EXISTS position_change     INTEGER;

CREATE INDEX IF NOT EXISTS idx_competitor_rankings_new_entrants
    ON competitor_rankings (date DESC, keyword_id)
    WHERE is_new_entrant = TRUE;

CREATE INDEX IF NOT EXISTS idx_competitor_rankings_top10
    ON competitor_rankings (keyword_id, date DESC)
    WHERE rank_position BETWEEN 1 AND 10;

-- ---------------------------------------------------------------------------
-- 4. keyword_serp_snapshots: per-keyword SERP context
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS keyword_serp_snapshots (
    id                     BIGSERIAL PRIMARY KEY,
    keyword_id             BIGINT       NOT NULL REFERENCES keywords(id) ON DELETE CASCADE,
    date                   DATE         NOT NULL,
    location_code          INTEGER      NOT NULL DEFAULT 2840,    -- DataForSEO: 2840 = United States
    device                 TEXT         NOT NULL DEFAULT 'desktop'
                           CHECK (device IN ('desktop', 'mobile')),
    serp_features          JSONB        NOT NULL DEFAULT '[]'::jsonb,
    ai_overview_present    BOOLEAN      NOT NULL DEFAULT FALSE,
    ai_overview_citations  JSONB        NOT NULL DEFAULT '[]'::jsonb,  -- array of {domain, url, position}
    total_results_seen     INTEGER,
    damco_position         INTEGER,                                      -- denormalized convenience
    damco_url              TEXT,
    top_10_domains         JSONB        NOT NULL DEFAULT '[]'::jsonb,    -- denormalized array for fast reads
    raw_payload_ref        TEXT,                                         -- optional pointer to archived raw response
    created_at             TIMESTAMPTZ  NOT NULL DEFAULT now(),
    UNIQUE (keyword_id, date, device)
);

CREATE INDEX IF NOT EXISTS idx_kw_serp_snapshots_date
    ON keyword_serp_snapshots (date DESC);

CREATE INDEX IF NOT EXISTS idx_kw_serp_snapshots_kw_date
    ON keyword_serp_snapshots (keyword_id, date DESC);

CREATE INDEX IF NOT EXISTS idx_kw_serp_snapshots_ai_overview
    ON keyword_serp_snapshots (date DESC, keyword_id)
    WHERE ai_overview_present = TRUE;

CREATE INDEX IF NOT EXISTS idx_kw_serp_snapshots_top10_domains
    ON keyword_serp_snapshots USING GIN (top_10_domains);

CREATE INDEX IF NOT EXISTS idx_kw_serp_snapshots_features
    ON keyword_serp_snapshots USING GIN (serp_features);

-- ---------------------------------------------------------------------------
-- 5. competitor_serp_events: append-only change-log stream
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS competitor_serp_events (
    id              BIGSERIAL PRIMARY KEY,
    event_type      TEXT         NOT NULL
                    CHECK (event_type IN (
                        'new_entrant',
                        'drop_out',
                        'position_gain',
                        'position_drop',
                        'damco_position_change',
                        'damco_enters_top_n',
                        'damco_drops_top_n',
                        'serp_feature_appeared',
                        'serp_feature_disappeared',
                        'threat_tier_changed',
                        'first_seen_anywhere'
                    )),
    keyword_id      BIGINT       REFERENCES keywords(id)    ON DELETE CASCADE,
    competitor_id   BIGINT       REFERENCES competitors(id) ON DELETE CASCADE,
    event_date      DATE         NOT NULL,
    old_value       JSONB        NOT NULL DEFAULT '{}'::jsonb,
    new_value       JSONB        NOT NULL DEFAULT '{}'::jsonb,
    delta           INTEGER,
    severity        TEXT         NOT NULL DEFAULT 'info'
                    CHECK (severity IN ('critical', 'high', 'medium', 'low', 'info')),
    metadata        JSONB        NOT NULL DEFAULT '{}'::jsonb,
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_serp_events_date
    ON competitor_serp_events (event_date DESC);

CREATE INDEX IF NOT EXISTS idx_serp_events_keyword
    ON competitor_serp_events (keyword_id, event_date DESC);

CREATE INDEX IF NOT EXISTS idx_serp_events_competitor
    ON competitor_serp_events (competitor_id, event_date DESC);

CREATE INDEX IF NOT EXISTS idx_serp_events_priority
    ON competitor_serp_events (severity, event_date DESC)
    WHERE severity IN ('critical', 'high');

CREATE INDEX IF NOT EXISTS idx_serp_events_type_date
    ON competitor_serp_events (event_type, event_date DESC);

-- ---------------------------------------------------------------------------
-- 6. mv_offering_competition: per-offering rollup (CONCURRENTLY-refreshable)
-- ---------------------------------------------------------------------------
-- Drop and recreate to keep the definition idempotent. Materialized view diffs
-- are awkward in PG; recreate is simpler and safe (rebuilds from base tables).

DROP MATERIALIZED VIEW IF EXISTS mv_offering_competition;

CREATE MATERIALIZED VIEW mv_offering_competition AS
WITH latest_snapshot_per_keyword AS (
    SELECT keyword_id, MAX(date) AS latest_date
    FROM competitor_rankings
    GROUP BY keyword_id
),
latest_rankings AS (
    SELECT cr.*
    FROM competitor_rankings cr
    JOIN latest_snapshot_per_keyword l
      ON l.keyword_id = cr.keyword_id
     AND l.latest_date = cr.date
)
SELECT
    k.offering                                                   AS offering,
    c.id                                                          AS competitor_id,
    c.competitor_domain                                           AS competitor_domain,
    c.company_name                                                AS company_name,
    c.category                                                    AS category,
    c.threat_tier                                                 AS threat_tier,
    COUNT(DISTINCT lr.keyword_id) FILTER (WHERE lr.rank_position BETWEEN 1 AND 10)
                                                                  AS keywords_in_top_10,
    (SELECT COUNT(*) FROM keywords k2
       WHERE k2.offering = k.offering AND k2.status = 'active')  AS total_keywords_in_offering,
    ROUND(
        100.0 * COUNT(DISTINCT lr.keyword_id) FILTER (WHERE lr.rank_position BETWEEN 1 AND 10)
        / NULLIF((SELECT COUNT(*) FROM keywords k2
                    WHERE k2.offering = k.offering AND k2.status = 'active'), 0),
        2
    )                                                             AS share_of_voice_pct,
    ROUND(AVG(lr.rank_position) FILTER (WHERE lr.rank_position BETWEEN 1 AND 10)::numeric, 2)
                                                                  AS avg_top_10_position,
    MIN(lr.rank_position)                                         AS best_position,
    MAX(lr.date)                                                  AS last_seen_date
FROM keywords k
JOIN latest_rankings lr ON lr.keyword_id = k.id
JOIN competitors c      ON c.id = lr.competitor_id
WHERE k.status = 'active'
  AND c.is_tracked = TRUE
  AND lr.rank_position BETWEEN 1 AND 10
GROUP BY k.offering, c.id, c.competitor_domain, c.company_name, c.category, c.threat_tier;

-- Unique index required for REFRESH MATERIALIZED VIEW CONCURRENTLY.
CREATE UNIQUE INDEX IF NOT EXISTS uq_mv_offering_competition
    ON mv_offering_competition (offering, competitor_id);

CREATE INDEX IF NOT EXISTS idx_mv_offering_competition_sov
    ON mv_offering_competition (offering, share_of_voice_pct DESC);

CREATE INDEX IF NOT EXISTS idx_mv_offering_competition_threat
    ON mv_offering_competition (offering, threat_tier);

COMMENT ON MATERIALIZED VIEW mv_offering_competition IS
    'Per-offering competitor rollup. Refresh after each snapshot cycle: REFRESH MATERIALIZED VIEW CONCURRENTLY mv_offering_competition;';

-- ---------------------------------------------------------------------------
-- 7. Helper function: recompute_competitor_aggregates(competitor_id)
--    Recomputes keyword_appearance_count, offering_appearance_count, threat_tier
--    based on the most-recent snapshot per keyword.
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION recompute_competitor_aggregates(p_competitor_id BIGINT)
RETURNS VOID AS $$
DECLARE
    v_keyword_count   INTEGER;
    v_offering_count  INTEGER;
    v_category        TEXT;
    v_old_tier        TEXT;
    v_new_tier        TEXT;
BEGIN
    -- Pull category and current tier
    SELECT category, threat_tier
      INTO v_category, v_old_tier
      FROM competitors
     WHERE id = p_competitor_id;

    -- Count keywords currently in top 10 (most recent snapshot per keyword)
    WITH latest AS (
        SELECT keyword_id, MAX(date) AS latest_date
          FROM competitor_rankings
         WHERE competitor_id = p_competitor_id
         GROUP BY keyword_id
    )
    SELECT COUNT(DISTINCT cr.keyword_id)
      INTO v_keyword_count
      FROM competitor_rankings cr
      JOIN latest l
        ON l.keyword_id = cr.keyword_id
       AND l.latest_date = cr.date
     WHERE cr.competitor_id = p_competitor_id
       AND cr.rank_position BETWEEN 1 AND 10;

    -- Count distinct offerings the competitor appears in (top 10, latest snapshot)
    WITH latest AS (
        SELECT keyword_id, MAX(date) AS latest_date
          FROM competitor_rankings
         WHERE competitor_id = p_competitor_id
         GROUP BY keyword_id
    )
    SELECT COUNT(DISTINCT k.offering)
      INTO v_offering_count
      FROM competitor_rankings cr
      JOIN latest l
        ON l.keyword_id = cr.keyword_id
       AND l.latest_date = cr.date
      JOIN keywords k
        ON k.id = cr.keyword_id
     WHERE cr.competitor_id = p_competitor_id
       AND cr.rank_position BETWEEN 1 AND 10
       AND k.offering IS NOT NULL;

    -- Compute new threat tier
    v_new_tier :=
        CASE
            WHEN v_old_tier = 'ignore'                           THEN 'ignore'
            WHEN (v_keyword_count >= 5 AND v_category IN ('direct', 'aggregator'))
              OR (v_offering_count >= 2 AND v_category = 'direct') THEN 'primary'
            WHEN v_keyword_count >= 2                            THEN 'watch'
            WHEN v_keyword_count = 1                             THEN 'peripheral'
            ELSE 'peripheral'
        END;

    UPDATE competitors
       SET keyword_appearance_count  = v_keyword_count,
           offering_appearance_count = v_offering_count,
           threat_tier               = v_new_tier
     WHERE id = p_competitor_id;

    -- Emit threat_tier_changed event if tier changed
    IF v_old_tier IS DISTINCT FROM v_new_tier THEN
        INSERT INTO competitor_serp_events
            (event_type, competitor_id, event_date, old_value, new_value, severity, metadata)
        VALUES (
            'threat_tier_changed',
            p_competitor_id,
            CURRENT_DATE,
            jsonb_build_object('threat_tier', v_old_tier),
            jsonb_build_object('threat_tier', v_new_tier),
            CASE WHEN v_new_tier = 'primary' THEN 'high' ELSE 'medium' END,
            jsonb_build_object(
                'keyword_appearance_count', v_keyword_count,
                'offering_appearance_count', v_offering_count
            )
        );
    END IF;
END;
$$ LANGUAGE plpgsql;

COMMENT ON FUNCTION recompute_competitor_aggregates(BIGINT) IS
    'Recomputes keyword_appearance_count, offering_appearance_count, and threat_tier for a competitor. Called by rank_tracker after each snapshot cycle.';

COMMIT;

-- ---------------------------------------------------------------------------
-- Post-migration: refresh the materialized view (no-op when base tables empty).
-- Run separately (outside transaction):
--     REFRESH MATERIALIZED VIEW CONCURRENTLY mv_offering_competition;
-- ---------------------------------------------------------------------------
