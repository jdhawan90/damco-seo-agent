-- ============================================================================
-- Damco SEO AI Agent System — seed correction + agent registry (migration 014)
--
-- Part 1 corrects a defect in migration 012.
-- Part 2 adds the `agents` table backing common/agents.py.
--
-- Idempotent.
-- ============================================================================

BEGIN;

-- ---------------------------------------------------------------------------
-- Part 1 — restore the three SERP-classification domain lists verbatim.
--
-- Migration 012 promised to reproduce the values the code held, so that
-- adopting the tenant profile changed no behaviour. For offerings, commercial
-- tokens and generic heads it did — those were copied and verified element by
-- element. For these three it did not: they were re-curated from memory
-- instead of copied, which quietly changed how SERP results get classified.
--
-- What the drift would have done:
--   * `salesforce.com` and `oracle.com` were added to big_tech_domains. For a
--     client that SELLS Salesforce services, auto-labelling salesforce.com as
--     "big_tech" is worse than the original behaviour of leaving it NULL for a
--     human to categorise.
--   * `google.com`, `amazon.com`, `microsoft.com`, `apple.com` and the Meta /
--     Facebook developer domains were dropped, so those would no longer be
--     recognised as big tech at all.
--   * `forbes.com` and `techcrunch.com` were dropped from aggregators.
--
-- Blast radius was limited — `upsert_competitor` COALESCEs onto the existing
-- category, so only newly-stubbed competitors would have been affected — but
-- "limited" is not "none", and the contract was no change.
--
-- Improving these lists may well be worth doing. It should be a deliberate
-- decision, not a side effect of a refactor.
-- ---------------------------------------------------------------------------

DELETE FROM tenant_vocabularies
 WHERE tenant_id = active_tenant()
   AND kind IN ('big_tech_domains', 'aggregator_domains', 'informational_domains');

INSERT INTO tenant_vocabularies (tenant_id, kind, term)
SELECT active_tenant(), 'big_tech_domains', unnest(ARRAY[
    'cloud.google.com', 'google.com', 'developers.google.com',
    'aws.amazon.com', 'amazon.com',
    'learn.microsoft.com', 'microsoft.com', 'azure.microsoft.com',
    'developer.apple.com', 'apple.com',
    'openai.com', 'anthropic.com',
    'meta.com', 'developers.facebook.com'])
ON CONFLICT (tenant_id, kind, term) DO NOTHING;

INSERT INTO tenant_vocabularies (tenant_id, kind, term)
SELECT active_tenant(), 'aggregator_domains', unnest(ARRAY[
    'g2.com', 'capterra.com', 'gartner.com', 'forrester.com',
    'trustradius.com', 'softwareadvice.com',
    'clutch.co', 'goodfirms.co', 'designrush.com',
    'forbes.com', 'techcrunch.com'])
ON CONFLICT (tenant_id, kind, term) DO NOTHING;

-- 'en.wikipedia.org' is in the original set but redundant: the caller already
-- falls back to `domain.endswith(".wikipedia.org")`. Kept anyway — the point
-- of this migration is fidelity to the original, not tidying it.
INSERT INTO tenant_vocabularies (tenant_id, kind, term)
SELECT active_tenant(), 'informational_domains', unnest(ARRAY[
    'wikipedia.org', 'en.wikipedia.org',
    'youtube.com', 'reddit.com', 'quora.com', 'medium.com'])
ON CONFLICT (tenant_id, kind, term) DO NOTHING;


-- ---------------------------------------------------------------------------
-- Part 2 — the agent registry.
--
-- Mirrors the CATALOGUE in common/agents.py so the inventory is queryable
-- alongside `agent_runs` instead of being retyped into markdown tables that
-- drift. `common/agents.py --sync` populates it; `--validate` keeps the
-- catalogue honest against the filesystem.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS agents (
    name         TEXT PRIMARY KEY,      -- matches agent_runs.agent_name
    title        TEXT        NOT NULL,
    folder       TEXT        NOT NULL,
    module       TEXT        NOT NULL,

    -- 'deterministic' is a first-class status, not a gap waiting to be filled.
    kind         TEXT        NOT NULL CHECK (kind IN ('deterministic', 'ai_assisted')),

    summary      TEXT        NOT NULL,
    reads        TEXT[]      NOT NULL DEFAULT '{}',
    writes       TEXT[]      NOT NULL DEFAULT '{}',

    llm_tier     TEXT        CHECK (llm_tier IN ('cheap', 'default', 'complex')),
    cadence_days INTEGER,
    blocked_by   TEXT,                  -- external dependency, if any
    notes        TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- Enforce in the schema what the dataclass enforces in Python: if it
    -- calls a model it is ai_assisted, and if it does not it declares no tier.
    CONSTRAINT agents_tier_matches_kind CHECK (
        (kind = 'ai_assisted'   AND llm_tier IS NOT NULL) OR
        (kind = 'deterministic' AND llm_tier IS NULL)
    )
);

CREATE INDEX IF NOT EXISTS idx_agents_folder ON agents (folder);

COMMENT ON TABLE agents IS
    'Inventory of every agent. Populated from common/agents.py CATALOGUE.';


-- Catalogue joined to reality. This is what the agent-directory table in
-- CLAUDE.md should always have been: derived from run history, not retyped.
CREATE OR REPLACE VIEW v_agent_status AS
SELECT a.name,
       a.title,
       a.folder,
       a.kind,
       a.blocked_by,
       a.cadence_days,
       r.run_date          AS last_run,
       r.status            AS last_status,
       r.records_processed AS last_records,
       (r.run_date IS NULL) AS never_run,
       CASE
           WHEN r.run_date IS NULL AND a.blocked_by IS NOT NULL THEN 'blocked'
           WHEN r.run_date IS NULL                              THEN 'never run'
           WHEN a.cadence_days IS NULL                          THEN 'on demand'
           -- agent_runs.run_date is a timestamp, so cast before subtracting
           -- or the result is an interval and the comparison has no operator.
           WHEN CURRENT_DATE - r.run_date::date > a.cadence_days * 2 THEN 'overdue'
           ELSE 'current'
       END AS health
  FROM agents a
  LEFT JOIN LATERAL (
      SELECT run_date, status, records_processed
        FROM agent_runs
       WHERE agent_name = a.name
       ORDER BY run_date DESC
       LIMIT 1
  ) r ON TRUE;

COMMIT;
