-- ============================================================================
-- GA4 behaviour metrics (migration 018)
--
-- Closes the loop Search Console cannot. GSC says we rank and people clicked;
-- only GA4 says what happened after the click. Ranking #3 for a keyword that
-- produces no enquiries is a vanity metric, and until now nothing in this
-- system could tell that apart from a win.
--
-- Grain
-- -----
-- One row per (landing page, window end date, channel). Deliberately NOT one
-- row per session or per day-page: the Data API is quota'd, the dashboard reads
-- rolling windows, and a per-day grain would multiply rows for precision no
-- tile displays.
--
-- Joining to the rest of the system
-- ---------------------------------
-- `landing_page` is what GA4 reports — usually a path, sometimes with query
-- string. `page_id` is resolved against `pages` on insert where a match exists,
-- so offerings and keywords become reachable. It stays NULL rather than
-- guessing when no page matches; a wrong join here would attribute one
-- offering's conversions to another.
--
-- Idempotent.
-- ============================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS ga4_landing_pages (
    id                BIGSERIAL PRIMARY KEY,

    -- End of the reporting window, not the run date. GA4 data is not final for
    -- ~48h, so the agent ends its window before that and this column records
    -- the period the numbers describe rather than when we asked.
    window_end        DATE        NOT NULL,
    window_days       INTEGER     NOT NULL,

    landing_page      TEXT        NOT NULL,
    channel           TEXT        NOT NULL DEFAULT 'Organic Search',

    -- NULL when no `pages` row matches. Never guessed.
    page_id           INTEGER     REFERENCES pages(id) ON DELETE SET NULL,

    sessions          INTEGER     NOT NULL DEFAULT 0,
    engaged_sessions  INTEGER     NOT NULL DEFAULT 0,
    engagement_rate   NUMERIC(6,4),
    conversions       NUMERIC(12,2) NOT NULL DEFAULT 0,
    revenue           NUMERIC(14,2) NOT NULL DEFAULT 0,
    avg_duration_sec  NUMERIC(10,2),

    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),

    UNIQUE (window_end, window_days, landing_page, channel)
);

CREATE INDEX IF NOT EXISTS idx_ga4_lp_window   ON ga4_landing_pages (window_end DESC);
CREATE INDEX IF NOT EXISTS idx_ga4_lp_page     ON ga4_landing_pages (page_id)
    WHERE page_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_ga4_lp_sessions ON ga4_landing_pages (sessions DESC);

COMMENT ON TABLE ga4_landing_pages IS
    'Per-landing-page GA4 behaviour over a rolling window. Written by '
    'keyword_intelligence.ga4_sync. Organic Search only unless the agent is '
    'run with --all-channels.';


-- Channel totals give organic a denominator. "SEO drove 40 conversions" only
-- means something next to what the other channels drove.
CREATE TABLE IF NOT EXISTS ga4_channel_totals (
    id               BIGSERIAL PRIMARY KEY,
    window_end       DATE        NOT NULL,
    window_days      INTEGER     NOT NULL,
    channel          TEXT        NOT NULL,
    sessions         INTEGER     NOT NULL DEFAULT 0,
    engaged_sessions INTEGER     NOT NULL DEFAULT 0,
    conversions      NUMERIC(12,2) NOT NULL DEFAULT 0,
    revenue          NUMERIC(14,2) NOT NULL DEFAULT 0,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (window_end, window_days, channel)
);


-- Organic behaviour rolled up to the offering, via pages.
--
-- This is the view that answers "is SEO working" rather than "are we ranking".
-- Offerings with tracked keywords but no organic sessions are the interesting
-- rows, so the join runs from `pages` outward rather than from GA4 inward.
CREATE OR REPLACE VIEW v_ga4_offering_performance AS
WITH latest AS (
    SELECT max(window_end) AS window_end FROM ga4_landing_pages
)
SELECT p.offering,
       count(DISTINCT g.landing_page)          AS landing_pages,
       COALESCE(sum(g.sessions), 0)            AS sessions,
       COALESCE(sum(g.engaged_sessions), 0)    AS engaged_sessions,
       COALESCE(sum(g.conversions), 0)         AS conversions,
       COALESCE(sum(g.revenue), 0)             AS revenue,
       CASE WHEN COALESCE(sum(g.sessions), 0) > 0
            THEN round(100.0 * sum(g.conversions) / sum(g.sessions), 2)
       END                                     AS conversion_rate_pct,
       (SELECT window_end FROM latest)         AS window_end
  FROM pages p
  LEFT JOIN ga4_landing_pages g
         ON g.page_id = p.id
        AND g.window_end = (SELECT window_end FROM latest)
        AND g.channel = 'Organic Search'
 WHERE p.offering IS NOT NULL
 GROUP BY p.offering;

COMMIT;
