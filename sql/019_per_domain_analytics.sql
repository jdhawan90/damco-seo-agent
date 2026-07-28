-- ============================================================================
-- Per-domain analytics identifiers (migration 019)
--
-- Why this changes shape
-- ----------------------
-- GA4 and GSC identifiers were about to be single environment variables:
-- GA4_PROPERTY_ID and GSC_SITE_URL. That is wrong for this tenant and would
-- have been wrong for most.
--
-- This tenant owns three domains and has TWO GA4 properties and TWO verified
-- GSC properties:
--
--   damcogroup.com     GA4 273666331   GSC https://www.damcogroup.com/
--   achieva.ai         GA4 497164582   GSC https://achieva.ai/
--   damcodigital.com   GA4 (none)      GSC (not verified)
--
-- With a single env var, Achieva's analytics and Search Console data were
-- invisible to the entire system — roughly a third of the search estate,
-- already collected by Google and simply never read.
--
-- An identifier that varies per domain belongs on the domain row, not in the
-- process environment. This also means adding a fourth property later is a
-- data change rather than a code change, which is the whole point of the
-- tenant profile.
--
-- The env vars remain as a fallback for a single-property deployment, so
-- nothing breaks for anyone who set them.
--
-- Idempotent.
-- ============================================================================

BEGIN;

ALTER TABLE tenant_domains
    ADD COLUMN IF NOT EXISTS ga4_property_id TEXT,
    ADD COLUMN IF NOT EXISTS gsc_site_url    TEXT;

COMMENT ON COLUMN tenant_domains.ga4_property_id IS
    'Numeric GA4 property id for this domain. NULL means no GA4 property — the '
    'sync agent skips the domain rather than guessing.';
COMMENT ON COLUMN tenant_domains.gsc_site_url IS
    'Exact Search Console property URL, trailing slash included. Must match the '
    'verified property string exactly or the API returns nothing.';

UPDATE tenant_domains
   SET ga4_property_id = '273666331',
       gsc_site_url    = 'https://www.damcogroup.com/'
 WHERE tenant_id = active_tenant() AND domain = 'damcogroup.com';

UPDATE tenant_domains
   SET ga4_property_id = '497164582',
       gsc_site_url    = 'https://achieva.ai/'
 WHERE tenant_id = active_tenant() AND domain = 'achieva.ai';

-- damcodigital.com deliberately left NULL on both. It has no GA4 property and
-- is not verified in Search Console. Leaving it NULL makes the agents skip it
-- explicitly and report that they did, rather than silently reporting zero
-- traffic for a property nobody ever connected.


-- ---------------------------------------------------------------------------
-- The GA4 tables need to know which property a row came from.
--
-- Two properties means /contact/ can legitimately appear twice with different
-- numbers. Without a domain column the unique key would collapse them into one
-- row and the second write would overwrite the first — silently reporting one
-- property's traffic as the whole estate.
-- ---------------------------------------------------------------------------

ALTER TABLE ga4_landing_pages
    ADD COLUMN IF NOT EXISTS domain TEXT;
ALTER TABLE ga4_channel_totals
    ADD COLUMN IF NOT EXISTS domain TEXT;

-- Existing rows predate multi-property support, so they can only be the
-- primary domain. The tables are empty today; this is for correctness if that
-- changes before the migration is applied elsewhere.
UPDATE ga4_landing_pages SET domain = (SELECT primary_domain FROM tenants WHERE id = active_tenant())
 WHERE domain IS NULL;
UPDATE ga4_channel_totals SET domain = (SELECT primary_domain FROM tenants WHERE id = active_tenant())
 WHERE domain IS NULL;

ALTER TABLE ga4_landing_pages  ALTER COLUMN domain SET NOT NULL;
ALTER TABLE ga4_channel_totals ALTER COLUMN domain SET NOT NULL;

-- Replace the unique keys to include domain.
ALTER TABLE ga4_landing_pages
    DROP CONSTRAINT IF EXISTS ga4_landing_pages_window_end_window_days_landing_page_channe_key;
ALTER TABLE ga4_channel_totals
    DROP CONSTRAINT IF EXISTS ga4_channel_totals_window_end_window_days_channel_key;

CREATE UNIQUE INDEX IF NOT EXISTS uq_ga4_lp_domain_window
    ON ga4_landing_pages (domain, window_end, window_days, landing_page, channel);
CREATE UNIQUE INDEX IF NOT EXISTS uq_ga4_ch_domain_window
    ON ga4_channel_totals (domain, window_end, window_days, channel);


-- Rebuild the offering view to carry the domain through, so a per-offering
-- number can be traced to the property it came from.
--
-- DROP then CREATE, not CREATE OR REPLACE: replace can only append columns to
-- the end of a view, and this adds `domains` in the middle, which Postgres
-- rejects as renaming `sessions`. Nothing depends on this view yet, so a drop
-- is safe; once something does, the new column would have to go last instead.
DROP VIEW IF EXISTS v_ga4_offering_performance;

CREATE VIEW v_ga4_offering_performance AS
WITH latest AS (
    SELECT max(window_end) AS window_end FROM ga4_landing_pages
)
SELECT p.offering,
       count(DISTINCT g.landing_page)       AS landing_pages,
       count(DISTINCT g.domain)             AS domains,
       COALESCE(sum(g.sessions), 0)         AS sessions,
       COALESCE(sum(g.engaged_sessions), 0) AS engaged_sessions,
       COALESCE(sum(g.conversions), 0)      AS conversions,
       COALESCE(sum(g.revenue), 0)          AS revenue,
       CASE WHEN COALESCE(sum(g.sessions), 0) > 0
            THEN round(100.0 * sum(g.conversions) / sum(g.sessions), 2)
       END                                  AS conversion_rate_pct,
       (SELECT window_end FROM latest)      AS window_end
  FROM pages p
  LEFT JOIN ga4_landing_pages g
         ON g.page_id = p.id
        AND g.window_end = (SELECT window_end FROM latest)
        AND g.channel = 'Organic Search'
 WHERE p.offering IS NOT NULL
 GROUP BY p.offering;

COMMIT;
