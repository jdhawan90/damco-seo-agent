-- ============================================================================
-- Damco SEO AI Agent System — trend source corrections (migration 011)
--
-- The first live harvest against migration 010's registry found two dead
-- feeds. Recorded here rather than edited into 010, because 010 has already
-- been applied and rewriting an applied migration makes the two environments
-- silently disagree.
--
-- Findings from the 2026-07-27 harvest:
--
--   Healthcare IT News   HTTP 403 — the origin blocks non-browser
--                        User-Agents outright. Not a rate limit; retrying
--                        won't help. Retired and replaced.
--   Digital Insurance    dig-in.com/feed now serves an HTML page, not XML.
--                        The feed has been retired upstream. Replaced.
--   Computerworld        Transient read timeout only; the feed is healthy.
--                        Fixed in code (feeds.py now retries once), not here.
--   Reddit (12 of 13)    HTTP 429. Our own pacing was too aggressive at 2s.
--                        Fixed in code (REDDIT_RATE_LIMIT_SEC = 6.5), not here.
--
-- Idempotent.
-- ============================================================================

BEGIN;

-- ---------------------------------------------------------------------------
-- Retire the two dead feeds.
--
-- Disabled rather than deleted: trend_mentions rows reference them, and the
-- evidence trail behind an already-scored candidate should stay readable.
-- The harvester skips disabled sources.
-- ---------------------------------------------------------------------------

UPDATE trend_sources
   SET enabled     = FALSE,
       last_status = 'blocked',
       last_error  = 'HTTP 403 — origin blocks non-browser User-Agents. '
                     'Retired 2026-07-27; replaced by Fierce Healthcare + Healthcare Dive.',
       updated_at  = now()
 WHERE url = 'https://www.healthcareitnews.com/home/feed';

UPDATE trend_sources
   SET enabled     = FALSE,
       last_status = 'error',
       last_error  = 'Feed retired upstream — the URL now serves HTML. '
                     'Retired 2026-07-27; replaced by Insurance Business Mag + Risk & Insurance.',
       updated_at  = now()
 WHERE url = 'https://www.dig-in.com/feed';

-- ---------------------------------------------------------------------------
-- Replacements, verified against the live endpoints on 2026-07-27.
-- ---------------------------------------------------------------------------

INSERT INTO trend_sources (name, url, source_type, category, offering_hint, weight) VALUES
    ('Fierce Healthcare',      'https://www.fiercehealthcare.com/rss/xml',      'rss', 'tech_press', 'Healthcare', 1.20),
    ('Healthcare Dive',        'https://www.healthcaredive.com/feeds/news/',    'rss', 'tech_press', 'Healthcare', 1.10),
    ('Insurance Business Mag', 'https://www.insurancebusinessmag.com/us/rss/',  'rss', 'tech_press', 'Insurance',  1.10),
    ('Risk & Insurance',       'https://riskandinsurance.com/feed/',            'rss', 'tech_press', 'Insurance',  1.10)
ON CONFLICT (url) DO NOTHING;

COMMIT;
