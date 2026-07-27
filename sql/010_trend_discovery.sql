-- ============================================================================
-- Damco SEO AI Agent System — trend discovery (migration 010)
--
-- Backs keyword_intelligence.trend_scout: the module that harvests industry
-- discussion (tech press, community forums, blogging platforms), extracts
-- emerging phrases, and proposes them as new keywords to track.
--
-- Three tables, one per pipeline stage:
--
--   trend_sources     — the feed registry. What we poll, how often, last status.
--                       Editable by operators via SQL; no code change needed to
--                       add or retire a source.
--   trend_mentions    — every item harvested from those feeds. Deduped by
--                       content hash so re-running the same day doesn't inflate
--                       mention counts. This is the evidence trail behind every
--                       candidate.
--   keyword_candidates — the deliverable. One row per proposed keyword, with
--                       search volume, momentum, scoring, and a human review
--                       status. Promotion into `keywords` is human-gated:
--                       nothing here reaches the tracked set automatically.
--
-- Why candidates don't live in `keywords`:
--   `keywords` is the tracked set — every row there costs money on every
--   rank-tracker run (~$0.00465/keyword/run). Candidates are speculative.
--   Keeping them separate means discovery can be noisy and cheap while the
--   tracked set stays curated and expensive.
--
-- Idempotent.
-- ============================================================================

BEGIN;

-- ---------------------------------------------------------------------------
-- 1. trend_sources — the feed registry
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS trend_sources (
    id                    BIGSERIAL PRIMARY KEY,
    name                  TEXT        NOT NULL,
    url                   TEXT        NOT NULL,
    -- How the harvester should read this URL.
    --   rss         — RSS 2.0 or Atom feed (the overwhelming majority)
    --   reddit      — a subreddit .rss endpoint (same parser, different etiquette)
    --   hackernews  — the free HN Algolia search API
    source_type           TEXT        NOT NULL DEFAULT 'rss'
                          CHECK (source_type IN ('rss', 'reddit', 'hackernews')),
    -- Editorial weight class. Used in scoring: a phrase trending across
    -- tech_press AND community is a stronger signal than one confined to
    -- a single blogging platform.
    category              TEXT        NOT NULL DEFAULT 'tech_press'
                          CHECK (category IN ('tech_press', 'community', 'blog_platform', 'vendor_blog')),
    -- Optional pre-bias: when set, phrases from this source lean toward
    -- this offering during classification. NULL = no bias, classify freely.
    offering_hint         TEXT,
    -- Editorial trust, 0.5–1.5. Multiplies the buzz contribution of each
    -- mention. Techmeme (curated) outranks a raw Medium tag feed.
    weight                NUMERIC(4,2) NOT NULL DEFAULT 1.00
                          CHECK (weight >= 0 AND weight <= 5),
    enabled               BOOLEAN     NOT NULL DEFAULT TRUE,
    poll_frequency_hours  INTEGER     NOT NULL DEFAULT 24,

    -- Operational state, updated by every harvest run.
    last_polled_at        TIMESTAMPTZ,
    last_status           TEXT,          -- 'ok' | 'error' | 'empty' | 'blocked'
    last_error            TEXT,
    consecutive_failures  INTEGER     NOT NULL DEFAULT 0,
    items_seen_total      BIGINT      NOT NULL DEFAULT 0,

    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (url)
);

CREATE INDEX IF NOT EXISTS idx_trend_sources_enabled  ON trend_sources (enabled, last_polled_at);
CREATE INDEX IF NOT EXISTS idx_trend_sources_category ON trend_sources (category);

COMMENT ON TABLE trend_sources IS
    'Feed registry for keyword_intelligence.trend_scout. Add/retire sources with plain SQL — no code change required.';
COMMENT ON COLUMN trend_sources.weight IS
    'Editorial trust multiplier applied to each mention''s buzz contribution.';
COMMENT ON COLUMN trend_sources.consecutive_failures IS
    'Auto-incremented on harvest failure. trend_scout warns at 3 and suggests disabling at 10.';


-- ---------------------------------------------------------------------------
-- 2. trend_mentions — harvested items (the evidence trail)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS trend_mentions (
    id             BIGSERIAL PRIMARY KEY,
    source_id      BIGINT      NOT NULL REFERENCES trend_sources(id) ON DELETE CASCADE,
    item_url       TEXT,
    title          TEXT        NOT NULL,
    summary        TEXT,
    author         TEXT,
    published_at   TIMESTAMPTZ,
    -- sha256 of (normalized title + item_url). The dedupe key: the same
    -- article resurfacing in tomorrow's feed must not count twice.
    content_hash   TEXT        NOT NULL,
    harvested_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    run_date       DATE        NOT NULL DEFAULT CURRENT_DATE,
    UNIQUE (source_id, content_hash)
);

CREATE INDEX IF NOT EXISTS idx_trend_mentions_source     ON trend_mentions (source_id);
CREATE INDEX IF NOT EXISTS idx_trend_mentions_published  ON trend_mentions (published_at DESC NULLS LAST);
CREATE INDEX IF NOT EXISTS idx_trend_mentions_run_date   ON trend_mentions (run_date DESC);

COMMENT ON TABLE trend_mentions IS
    'Every item harvested from trend_sources. Deduped per source by content_hash so repeat runs do not inflate mention counts.';


-- ---------------------------------------------------------------------------
-- 3. keyword_candidates — proposed keywords awaiting human review
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS keyword_candidates (
    id                       BIGSERIAL PRIMARY KEY,

    -- Normalized (lowercased, whitespace-collapsed) form — the uniqueness key.
    candidate_keyword        TEXT        NOT NULL,
    -- What a human should read. Preserves original casing where meaningful.
    display_keyword          TEXT,
    -- When the LLM rewrote a raw buzz phrase into a search-shaped query
    -- ("agentic AI" -> "agentic ai development services"), the raw phrase
    -- is kept here so the evidence trail still makes sense.
    source_phrase            TEXT,

    -- ---- Classification -------------------------------------------------
    suggested_offering       TEXT,
    offering_confidence      NUMERIC(4,3),     -- 0.000–1.000
    classification_method    TEXT              -- 'rule' | 'llm' | 'source_hint'
                             CHECK (classification_method IS NULL
                                    OR classification_method IN ('rule', 'llm', 'source_hint')),
    intent                   TEXT,             -- informational | commercial | transactional | navigational

    -- ---- Buzz signal (from trend_mentions) -------------------------------
    mention_count            INTEGER     NOT NULL DEFAULT 0,
    source_spread            INTEGER     NOT NULL DEFAULT 0,   -- distinct trend_sources
    category_spread          INTEGER     NOT NULL DEFAULT 0,   -- distinct source categories
    first_seen_at            TIMESTAMPTZ,
    last_seen_at             TIMESTAMPTZ,

    -- ---- Search-volume signal (Google Ads Keyword Planner) ---------------
    search_volume            INTEGER,
    cpc                      NUMERIC(10,2),
    competition              TEXT,             -- LOW | MEDIUM | HIGH
    competition_index        INTEGER,          -- 0–100
    -- Raw 12-month array from Keyword Planner: [{year, month, search_volume}, ...]
    monthly_searches         JSONB,
    -- last 3 months mean / prior 9 months mean. >1.0 = rising.
    momentum_ratio           NUMERIC(8,3),
    volume_source            TEXT,             -- 'google_ads' | 'dataforseo_google_ads' | NULL
    volume_checked_at        TIMESTAMPTZ,

    -- ---- Novelty vs the tracked set --------------------------------------
    is_novel                 BOOLEAN     NOT NULL DEFAULT TRUE,
    nearest_tracked_keyword  TEXT,
    nearest_similarity       NUMERIC(4,3),     -- 0.000–1.000 token-set Jaccard

    -- ---- Scoring (all 0–100 except trend_score which is the weighted sum) -
    buzz_score               NUMERIC(6,2) NOT NULL DEFAULT 0,
    volume_score             NUMERIC(6,2) NOT NULL DEFAULT 0,
    momentum_score           NUMERIC(6,2) NOT NULL DEFAULT 0,
    opportunity_score        NUMERIC(6,2) NOT NULL DEFAULT 0,
    commercial_score         NUMERIC(6,2) NOT NULL DEFAULT 0,
    trend_score              NUMERIC(6,2) NOT NULL DEFAULT 0,

    -- ---- Evidence + review ------------------------------------------------
    -- [{title, url, source, category, published_at}, ...] — capped at 10 by
    -- the writer so the row stays readable.
    evidence                 JSONB       NOT NULL DEFAULT '[]'::jsonb,

    status                   TEXT        NOT NULL DEFAULT 'new'
                             CHECK (status IN ('new', 'reviewed', 'approved',
                                               'rejected', 'promoted', 'duplicate')),
    reviewed_by              TEXT,
    reviewed_at              TIMESTAMPTZ,
    review_note              TEXT,
    promoted_keyword_id      BIGINT      REFERENCES keywords(id) ON DELETE SET NULL,

    first_discovered_date    DATE        NOT NULL DEFAULT CURRENT_DATE,
    last_scored_date         DATE        NOT NULL DEFAULT CURRENT_DATE,
    created_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at               TIMESTAMPTZ NOT NULL DEFAULT now(),

    UNIQUE (candidate_keyword)
);

CREATE INDEX IF NOT EXISTS idx_kw_candidates_status      ON keyword_candidates (status);
CREATE INDEX IF NOT EXISTS idx_kw_candidates_score       ON keyword_candidates (trend_score DESC);
CREATE INDEX IF NOT EXISTS idx_kw_candidates_offering    ON keyword_candidates (suggested_offering, trend_score DESC);
CREATE INDEX IF NOT EXISTS idx_kw_candidates_discovered  ON keyword_candidates (first_discovered_date DESC);
CREATE INDEX IF NOT EXISTS idx_kw_candidates_novel       ON keyword_candidates (is_novel, status);

COMMENT ON TABLE keyword_candidates IS
    'Proposed keywords discovered by keyword_intelligence.trend_scout. Promotion into `keywords` is human-gated via --promote; nothing enters the tracked set automatically.';
COMMENT ON COLUMN keyword_candidates.momentum_ratio IS
    'Mean monthly search volume of the last 3 months divided by the prior 9. >1.0 means the term is rising.';
COMMENT ON COLUMN keyword_candidates.trend_score IS
    'Weighted composite: buzz 30 + volume 25 + momentum 20 + opportunity 15 + commercial 10.';


-- ---------------------------------------------------------------------------
-- Convenience view: the review queue an SEO lead actually looks at
-- ---------------------------------------------------------------------------

CREATE OR REPLACE VIEW v_trend_review_queue AS
SELECT
    c.id,
    c.display_keyword,
    c.suggested_offering,
    c.search_volume,
    c.momentum_ratio,
    c.mention_count,
    c.source_spread,
    c.competition,
    c.cpc,
    c.trend_score,
    c.status,
    c.first_discovered_date,
    jsonb_array_length(c.evidence) AS evidence_count
  FROM keyword_candidates c
 WHERE c.status IN ('new', 'reviewed')
   AND c.is_novel
 ORDER BY c.trend_score DESC;

COMMENT ON VIEW v_trend_review_queue IS
    'Un-actioned, novel keyword candidates ranked by trend_score. The default SEO-lead review surface.';


-- ---------------------------------------------------------------------------
-- Seed the feed registry
--
-- Curated to mirror Damco's 15 offerings. RSS/Atom and documented JSON APIs
-- only — no HTML scraping, so nothing breaks when a site restyles and we stay
-- inside every publisher's intended distribution channel.
--
-- ON CONFLICT DO NOTHING: re-running the migration never clobbers operator
-- edits to weight/enabled/poll_frequency_hours.
-- ---------------------------------------------------------------------------

INSERT INTO trend_sources (name, url, source_type, category, offering_hint, weight) VALUES
    -- ---- Tier-1 tech press (broad, high editorial trust) ----
    ('Techmeme',              'https://www.techmeme.com/feed.xml',                    'rss', 'tech_press',    NULL,                 1.40),
    ('CIO.com',               'https://www.cio.com/feed/',                            'rss', 'tech_press',    NULL,                 1.30),
    ('InfoWorld',             'https://www.infoworld.com/feed/',                      'rss', 'tech_press',    NULL,                 1.20),
    ('Computerworld',         'https://www.computerworld.com/feed/',                  'rss', 'tech_press',    NULL,                 1.10),
    ('CIO Dive',              'https://www.ciodive.com/feeds/news/',                  'rss', 'tech_press',    NULL,                 1.20),
    ('The New Stack',         'https://thenewstack.io/feed/',                         'rss', 'tech_press',    'Cloud',              1.20),
    ('The Register',          'https://www.theregister.com/headlines.atom',           'rss', 'tech_press',    NULL,                 1.10),
    ('InformationWeek',       'https://www.informationweek.com/rss.xml',              'rss', 'tech_press',    NULL,                 1.00),
    ('VentureBeat AI',        'https://venturebeat.com/category/ai/feed/',            'rss', 'tech_press',    'AI',                 1.20),
    ('diginomica',            'https://diginomica.com/feed',                          'rss', 'tech_press',    NULL,                 1.10),

    -- ---- Offering-aligned vendor / specialist blogs ----
    ('Salesforce Ben',        'https://www.salesforceben.com/feed/',                  'rss', 'vendor_blog',   'Salesforce',         1.20),
    ('Microsoft Azure Blog',  'https://azure.microsoft.com/en-us/blog/feed/',         'rss', 'vendor_blog',   'Microsoft',          1.10),
    ('AWS News Blog',         'https://aws.amazon.com/blogs/aws/feed/',               'rss', 'vendor_blog',   'Cloud',              1.00),
    ('IT Jungle (IBM i)',     'https://www.itjungle.com/feed/',                       'rss', 'vendor_blog',   'AS400',              1.20),
    ('Digital Insurance',     'https://www.dig-in.com/feed',                          'rss', 'tech_press',    'Insurance',          1.20),
    ('Healthcare IT News',    'https://www.healthcareitnews.com/home/feed',           'rss', 'tech_press',    'Healthcare',         1.20),
    ('CoinDesk',              'https://www.coindesk.com/arc/outboundfeeds/rss/',      'rss', 'tech_press',    'Web3',               0.90),

    -- ---- Blogging platforms (high noise, high novelty) ----
    ('Medium — AI',           'https://medium.com/feed/tag/artificial-intelligence',  'rss', 'blog_platform', 'AI',                 0.80),
    ('Medium — Salesforce',   'https://medium.com/feed/tag/salesforce',               'rss', 'blog_platform', 'Salesforce',         0.80),
    ('Medium — Cloud',        'https://medium.com/feed/tag/cloud-computing',          'rss', 'blog_platform', 'Cloud',              0.80),
    ('Medium — Data Eng',     'https://medium.com/feed/tag/data-engineering',         'rss', 'blog_platform', 'Data Engineering',   0.80),
    ('Medium — DevOps',       'https://medium.com/feed/tag/devops',                   'rss', 'blog_platform', 'App Dev',            0.80),
    ('Medium — InsurTech',    'https://medium.com/feed/tag/insurtech',                'rss', 'blog_platform', 'Insurance',          0.80),
    ('Medium — Low Code',     'https://medium.com/feed/tag/low-code',                 'rss', 'blog_platform', 'LC/NC',              0.80),
    ('Medium — RPA',          'https://medium.com/feed/tag/rpa',                      'rss', 'blog_platform', 'IPA',                0.80),
    ('Medium — Web3',         'https://medium.com/feed/tag/web3',                     'rss', 'blog_platform', 'Web3',               0.80),

    -- ---- Practitioner communities (earliest signal, noisiest) ----
    ('r/artificial',          'https://www.reddit.com/r/artificial/.rss',             'reddit', 'community', 'AI',                 0.90),
    ('r/MachineLearning',     'https://www.reddit.com/r/MachineLearning/.rss',        'reddit', 'community', 'AI',                 0.90),
    ('r/devops',              'https://www.reddit.com/r/devops/.rss',                 'reddit', 'community', 'App Dev',            0.90),
    ('r/salesforce',          'https://www.reddit.com/r/salesforce/.rss',             'reddit', 'community', 'Salesforce',         1.00),
    ('r/dataengineering',     'https://www.reddit.com/r/dataengineering/.rss',        'reddit', 'community', 'Data Engineering',   1.00),
    ('r/AZURE',               'https://www.reddit.com/r/AZURE/.rss',                  'reddit', 'community', 'Microsoft',          0.90),
    ('r/aws',                 'https://www.reddit.com/r/aws/.rss',                    'reddit', 'community', 'Cloud',              0.90),
    ('r/BusinessIntelligence','https://www.reddit.com/r/BusinessIntelligence/.rss',   'reddit', 'community', 'Data Engineering',   0.80),
    ('r/InsurTech',           'https://www.reddit.com/r/InsurTech/.rss',              'reddit', 'community', 'Insurance',          0.90),
    ('r/nocode',              'https://www.reddit.com/r/nocode/.rss',                 'reddit', 'community', 'LC/NC',              0.80),
    ('r/IBMi',                'https://www.reddit.com/r/IBMi/.rss',                   'reddit', 'community', 'AS400',              0.90),
    ('r/ITManagers',          'https://www.reddit.com/r/ITManagers/.rss',             'reddit', 'community', 'vCTO',               0.80),

    -- ---- Hacker News (free Algolia API — earliest signal of all) ----
    ('Hacker News',           'https://hn.algolia.com/api/v1/search_by_date',         'hackernews', 'community', NULL,             1.00)
ON CONFLICT (url) DO NOTHING;

COMMIT;
