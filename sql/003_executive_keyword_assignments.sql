-- ============================================================================
-- Migration 003: Executive-keyword assignment table
--
-- Maps SEO executives to the keywords they own. Only high-importance
-- keywords are tracked here. Source of truth is the master ranking Excel.
-- ============================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS seo_executives (
    id          BIGSERIAL PRIMARY KEY,
    name        TEXT         NOT NULL UNIQUE,
    email       TEXT,
    status      TEXT         NOT NULL DEFAULT 'active'
                CHECK (status IN ('active', 'inactive')),
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS executive_keyword_assignments (
    id              BIGSERIAL PRIMARY KEY,
    executive_id    BIGINT       NOT NULL REFERENCES seo_executives(id) ON DELETE CASCADE,
    keyword_id      BIGINT       NOT NULL REFERENCES keywords(id) ON DELETE CASCADE,
    sheet_source    TEXT,         -- 'Service Pages' or 'Tech Pages'
    assigned_at     TIMESTAMPTZ  NOT NULL DEFAULT now(),
    UNIQUE (executive_id, keyword_id)
);

CREATE INDEX IF NOT EXISTS idx_exec_kw_exec ON executive_keyword_assignments (executive_id);
CREATE INDEX IF NOT EXISTS idx_exec_kw_kw   ON executive_keyword_assignments (keyword_id);

-- Add importance column to keywords table if not present
ALTER TABLE keywords ADD COLUMN IF NOT EXISTS importance TEXT
    CHECK (importance IN ('high', 'medium', 'low'));

-- Add services column (sub-offering) to keywords table
ALTER TABLE keywords ADD COLUMN IF NOT EXISTS services TEXT;

-- Add google_sv column for Google Search Volume range
ALTER TABLE keywords ADD COLUMN IF NOT EXISTS google_sv TEXT;

COMMIT;
