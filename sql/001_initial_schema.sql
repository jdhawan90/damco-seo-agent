-- ============================================================================
-- Damco SEO AI Agent System — initial schema (migration 001)
--
-- Implements the functional groups described in the Technical Architecture
-- doc §3.2:
--     Keyword & Ranking       — keywords, keyword_rankings, keyword_search_volume
--     Page & Content          — pages, content_briefs, compliance_checks
--     Backlink & Off-Page     — backlinks, offpage_activities, platform_targets
--     Competitor              — competitors, competitor_rankings, competitor_changes
--     Technical SEO           — technical_issues, cwv_metrics, internal_links
--     System & Operations     — agent_runs, triggers, config
--
-- Design notes
--   * Every table has a surrogate BIGSERIAL primary key + natural-key
--     UNIQUE constraints where applicable (keeps FKs cheap and upserts easy).
--   * JSONB is used for payloads (briefs, issue details, trigger events) so
--     schema changes on the payload don't require migrations.
--   * All timestamps are TIMESTAMPTZ and default to now().
--   * Indexes cover the query shapes the agents will actually run:
--     latest-snapshot-per-keyword, trend-by-date, unresolved-issues, etc.
-- ============================================================================

BEGIN;

-- ---------------------------------------------------------------------------
-- Keyword & Ranking Data
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS keywords (
    id               BIGSERIAL PRIMARY KEY,
    keyword          TEXT         NOT NULL,
    offering         TEXT,                          -- e.g. "AI Development", "Insurance Broker Software"
    intent           TEXT,                          -- informational | commercial | transactional | navigational
    journey_stage    TEXT,                          -- awareness | consideration | decision
    type             TEXT         NOT NULL DEFAULT 'primary'
                     CHECK (type IN ('primary', 'secondary', 'longtail', 'brand')),
    target_url       TEXT,
    status           TEXT         NOT NULL DEFAULT 'active'
                     CHECK (status IN ('active', 'paused', 'archived')),
    created_at       TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ  NOT NULL DEFAULT now(),
    UNIQUE (keyword, offering)
);

CREATE INDEX IF NOT EXISTS idx_keywords_offering        ON keywords (offering) WHERE status = 'active';
CREATE INDEX IF NOT EXISTS idx_keywords_target_url      ON keywords (target_url);

CREATE TABLE IF NOT EXISTS keyword_rankings (
    id               BIGSERIAL PRIMARY KEY,
    keyword_id       BIGINT       NOT NULL REFERENCES keywords(id) ON DELETE CASCADE,
    date             DATE         NOT NULL,
    rank_position    INTEGER,                       -- NULL = not in top N searched
    rank_bucket      TEXT,                          -- '1-5', '5-10', '10-20', '20-50', '50+', 'not-found'
    search_volume    INTEGER,
    url_found        TEXT,
    source           TEXT         NOT NULL DEFAULT 'dataforseo'
                     CHECK (source IN ('dataforseo', 'gsc', 'manual')),
    created_at       TIMESTAMPTZ  NOT NULL DEFAULT now(),
    UNIQUE (keyword_id, date, source)
);

CREATE INDEX IF NOT EXISTS idx_keyword_rankings_date      ON keyword_rankings (date DESC);
CREATE INDEX IF NOT EXISTS idx_keyword_rankings_striking  ON keyword_rankings (date, rank_position)
    WHERE rank_position BETWEEN 11 AND 20;

CREATE TABLE IF NOT EXISTS keyword_search_volume (
    id                  BIGSERIAL PRIMARY KEY,
    keyword_id          BIGINT       NOT NULL REFERENCES keywords(id) ON DELETE CASCADE,
    date                DATE         NOT NULL,
    search_volume       INTEGER,
    keyword_difficulty  INTEGER,
    cpc                 NUMERIC(10,2),
    source              TEXT         NOT NULL DEFAULT 'dataforseo',
    created_at          TIMESTAMPTZ  NOT NULL DEFAULT now(),
    UNIQUE (keyword_id, date, source)
);

-- ---------------------------------------------------------------------------
-- Page & Content Data
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS pages (
    id                BIGSERIAL PRIMARY KEY,
    url               TEXT         NOT NULL UNIQUE,
    offering          TEXT,
    page_type         TEXT         NOT NULL DEFAULT 'blog'
                      CHECK (page_type IN ('pillar', 'service', 'blog', 'glossary', 'resource', 'landing', 'home')),
    title             TEXT,
    date_published    DATE,
    word_count        INTEGER,
    last_audited      TIMESTAMPTZ,
    created_at        TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_pages_offering   ON pages (offering);
CREATE INDEX IF NOT EXISTS idx_pages_page_type  ON pages (page_type);

CREATE TABLE IF NOT EXISTS content_briefs (
    id                BIGSERIAL PRIMARY KEY,
    page_id           BIGINT       REFERENCES pages(id) ON DELETE SET NULL,
    target_url        TEXT,                           -- populated even when page_id is NULL (new page)
    keywords_json     JSONB        NOT NULL DEFAULT '[]'::jsonb,
    brief_content     JSONB        NOT NULL,
    file_path         TEXT,                           -- path under outputs/briefs/
    status            TEXT         NOT NULL DEFAULT 'draft'
                      CHECK (status IN ('draft', 'approved', 'in_progress', 'delivered', 'rejected')),
    assigned_writer   TEXT,
    created_at        TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_content_briefs_status  ON content_briefs (status);
CREATE INDEX IF NOT EXISTS idx_content_briefs_page    ON content_briefs (page_id);

CREATE TABLE IF NOT EXISTS compliance_checks (
    id                    BIGSERIAL PRIMARY KEY,
    page_id               BIGINT       NOT NULL REFERENCES pages(id) ON DELETE CASCADE,
    check_date            TIMESTAMPTZ  NOT NULL DEFAULT now(),
    overall_score         NUMERIC(5,2),               -- 0.00 – 100.00
    issues_json           JSONB        NOT NULL DEFAULT '[]'::jsonb,
    keyword_density       NUMERIC(5,2),
    meta_status           TEXT,                        -- pass | warn | fail
    internal_links_count  INTEGER
);

CREATE INDEX IF NOT EXISTS idx_compliance_checks_page_date ON compliance_checks (page_id, check_date DESC);

-- ---------------------------------------------------------------------------
-- Backlink & Off-Page Data
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS backlinks (
    id                  BIGSERIAL PRIMARY KEY,
    page_id             BIGINT       REFERENCES pages(id) ON DELETE SET NULL,
    source_url          TEXT         NOT NULL,
    source_domain       TEXT         NOT NULL,
    domain_authority    INTEGER,
    link_type           TEXT
                        CHECK (link_type IN ('dofollow', 'nofollow', 'ugc', 'sponsored', 'unknown')),
    anchor_text         TEXT,
    date_discovered     DATE         NOT NULL DEFAULT CURRENT_DATE,
    data_source         TEXT         NOT NULL
                        CHECK (data_source IN ('dataforseo', 'gsc', 'manual')),
    UNIQUE (source_url, page_id, data_source)
);

CREATE INDEX IF NOT EXISTS idx_backlinks_domain    ON backlinks (source_domain);
CREATE INDEX IF NOT EXISTS idx_backlinks_page_date ON backlinks (page_id, date_discovered DESC);

CREATE TABLE IF NOT EXISTS platform_targets (
    id               BIGSERIAL PRIMARY KEY,
    platform_url     TEXT         NOT NULL UNIQUE,
    platform_name    TEXT,
    domain_authority INTEGER,
    niche            TEXT,
    contact_info     JSONB        DEFAULT '{}'::jsonb,
    response_rate    NUMERIC(5,2),                   -- 0.00 – 100.00
    quality_score    NUMERIC(5,2),
    last_contacted   TIMESTAMPTZ,
    status           TEXT         NOT NULL DEFAULT 'active'
                     CHECK (status IN ('active', 'blacklist', 'exhausted', 'pending')),
    created_at       TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS offpage_activities (
    id                BIGSERIAL PRIMARY KEY,
    executive         TEXT,
    activity_type     TEXT         NOT NULL
                      CHECK (activity_type IN ('guest_post', 'ugc', 'outreach', 'pr_pitch', 'paid_placement', 'follow_up', 'other')),
    target_page_id    BIGINT       REFERENCES pages(id) ON DELETE SET NULL,
    platform_id       BIGINT       REFERENCES platform_targets(id) ON DELETE SET NULL,
    platform          TEXT,
    status            TEXT         NOT NULL DEFAULT 'submitted'
                      CHECK (status IN ('draft', 'submitted', 'published', 'rejected', 'no_response')),
    date              DATE         NOT NULL DEFAULT CURRENT_DATE,
    published_url     TEXT,
    notes             TEXT,
    created_at        TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_offpage_activities_exec_date ON offpage_activities (executive, date DESC);
CREATE INDEX IF NOT EXISTS idx_offpage_activities_page      ON offpage_activities (target_page_id);

-- ---------------------------------------------------------------------------
-- Competitor Data
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS competitors (
    id                    BIGSERIAL PRIMARY KEY,
    competitor_domain     TEXT         NOT NULL UNIQUE,
    offering              TEXT,
    date_last_crawled     TIMESTAMPTZ,
    status                TEXT         NOT NULL DEFAULT 'active'
                          CHECK (status IN ('active', 'paused', 'archived')),
    notes                 TEXT,
    created_at            TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS competitor_rankings (
    id                BIGSERIAL PRIMARY KEY,
    competitor_id     BIGINT       NOT NULL REFERENCES competitors(id) ON DELETE CASCADE,
    keyword_id        BIGINT       NOT NULL REFERENCES keywords(id)    ON DELETE CASCADE,
    date              DATE         NOT NULL,
    rank_position     INTEGER,
    url_found         TEXT,
    created_at        TIMESTAMPTZ  NOT NULL DEFAULT now(),
    UNIQUE (competitor_id, keyword_id, date)
);

CREATE INDEX IF NOT EXISTS idx_competitor_rankings_kw_date ON competitor_rankings (keyword_id, date DESC);

CREATE TABLE IF NOT EXISTS competitor_changes (
    id                  BIGSERIAL PRIMARY KEY,
    competitor_id       BIGINT       NOT NULL REFERENCES competitors(id) ON DELETE CASCADE,
    url                 TEXT         NOT NULL,
    change_type         TEXT         NOT NULL
                        CHECK (change_type IN ('new_page', 'content_update', 'title_change', 'meta_change', 'structure_change', 'removed')),
    date_detected       TIMESTAMPTZ  NOT NULL DEFAULT now(),
    diff_summary        TEXT,
    significance_score  NUMERIC(3,2)                  -- 0.00 – 1.00
);

CREATE INDEX IF NOT EXISTS idx_competitor_changes_url_date ON competitor_changes (url, date_detected DESC);

-- ---------------------------------------------------------------------------
-- Technical SEO Data
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS technical_issues (
    id              BIGSERIAL PRIMARY KEY,
    url             TEXT         NOT NULL,
    issue_type      TEXT         NOT NULL,          -- broken_link | missing_meta | duplicate_content | canonical_issue | redirect_chain | schema_error | sitemap_gap
    severity        TEXT         NOT NULL DEFAULT 'medium'
                    CHECK (severity IN ('critical', 'high', 'medium', 'low', 'info')),
    date_found      TIMESTAMPTZ  NOT NULL DEFAULT now(),
    date_resolved   TIMESTAMPTZ,
    details         JSONB        NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_technical_issues_unresolved ON technical_issues (url, issue_type)
    WHERE date_resolved IS NULL;
CREATE INDEX IF NOT EXISTS idx_technical_issues_severity   ON technical_issues (severity, date_found DESC)
    WHERE date_resolved IS NULL;

CREATE TABLE IF NOT EXISTS cwv_metrics (
    id                 BIGSERIAL PRIMARY KEY,
    url                TEXT         NOT NULL,
    date               DATE         NOT NULL DEFAULT CURRENT_DATE,
    lcp_ms             INTEGER,                     -- Largest Contentful Paint
    inp_ms             INTEGER,                     -- Interaction to Next Paint
    cls_score          NUMERIC(6,4),                -- Cumulative Layout Shift
    performance_score  INTEGER,                     -- 0-100 from PageSpeed Insights
    device             TEXT         NOT NULL DEFAULT 'mobile'
                       CHECK (device IN ('mobile', 'desktop')),
    created_at         TIMESTAMPTZ  NOT NULL DEFAULT now(),
    UNIQUE (url, date, device)
);

CREATE INDEX IF NOT EXISTS idx_cwv_metrics_url_date ON cwv_metrics (url, date DESC);

CREATE TABLE IF NOT EXISTS internal_links (
    id            BIGSERIAL PRIMARY KEY,
    source_url    TEXT         NOT NULL,
    target_url    TEXT         NOT NULL,
    anchor_text   TEXT,
    date_crawled  TIMESTAMPTZ  NOT NULL DEFAULT now(),
    UNIQUE (source_url, target_url, anchor_text)
);

CREATE INDEX IF NOT EXISTS idx_internal_links_target ON internal_links (target_url);
CREATE INDEX IF NOT EXISTS idx_internal_links_source ON internal_links (source_url);

-- ---------------------------------------------------------------------------
-- System & Operations Data
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS agent_runs (
    id                  BIGSERIAL PRIMARY KEY,
    agent_name          TEXT         NOT NULL,
    run_date            TIMESTAMPTZ  NOT NULL DEFAULT now(),
    status              TEXT         NOT NULL
                        CHECK (status IN ('running', 'success', 'error', 'partial')),
    records_processed   INTEGER      DEFAULT 0,
    errors              JSONB        DEFAULT '[]'::jsonb,
    duration_seconds    NUMERIC(10,2),
    metadata            JSONB        DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_agent_runs_name_date ON agent_runs (agent_name, run_date DESC);

CREATE TABLE IF NOT EXISTS triggers (
    id             BIGSERIAL PRIMARY KEY,
    source_agent   TEXT         NOT NULL,
    target_agent   TEXT         NOT NULL,
    event_type     TEXT         NOT NULL,
    payload_json   JSONB        NOT NULL DEFAULT '{}'::jsonb,
    status         TEXT         NOT NULL DEFAULT 'pending'
                   CHECK (status IN ('pending', 'processing', 'processed', 'failed', 'skipped')),
    created_at     TIMESTAMPTZ  NOT NULL DEFAULT now(),
    processed_at   TIMESTAMPTZ,
    attempts       INTEGER      NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_triggers_pending ON triggers (target_agent, created_at)
    WHERE status = 'pending';

CREATE TABLE IF NOT EXISTS config (
    key            TEXT         PRIMARY KEY,
    value          JSONB        NOT NULL,
    description    TEXT,
    last_updated   TIMESTAMPTZ  NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- Updated-at trigger (applied to tables that track mutation history)
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DO $$
DECLARE
    t TEXT;
BEGIN
    FOR t IN SELECT unnest(ARRAY['keywords', 'pages', 'content_briefs']) LOOP
        EXECUTE format(
            'DROP TRIGGER IF EXISTS trg_%I_updated_at ON %I;
             CREATE TRIGGER trg_%I_updated_at
             BEFORE UPDATE ON %I
             FOR EACH ROW EXECUTE FUNCTION set_updated_at();',
            t, t, t, t
        );
    END LOOP;
END $$;

COMMIT;
