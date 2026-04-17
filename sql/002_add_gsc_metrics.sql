-- ============================================================================
-- Migration 002: Add GSC metrics columns to keyword_rankings
--
-- Enables storing Google Search Console data (clicks, impressions, CTR)
-- alongside DataForSEO SERP rankings in the same table. When source='gsc',
-- rank_position holds the GSC average position and these columns are populated.
-- When source='dataforseo', these columns remain NULL.
-- ============================================================================

BEGIN;

ALTER TABLE keyword_rankings
    ADD COLUMN IF NOT EXISTS clicks       INTEGER,
    ADD COLUMN IF NOT EXISTS impressions  INTEGER,
    ADD COLUMN IF NOT EXISTS ctr          NUMERIC(6,4);

COMMENT ON COLUMN keyword_rankings.clicks      IS 'Total clicks from GSC over the measurement period (NULL for DataForSEO rows)';
COMMENT ON COLUMN keyword_rankings.impressions IS 'Total impressions from GSC over the measurement period (NULL for DataForSEO rows)';
COMMENT ON COLUMN keyword_rankings.ctr         IS 'Click-through rate from GSC (0.0000 to 1.0000; NULL for DataForSEO rows)';

COMMIT;
