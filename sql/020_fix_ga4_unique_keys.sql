-- ============================================================================
-- Drop the pre-multi-property GA4 unique keys (migration 020)
--
-- Migration 019 tried to drop them by name and missed. Postgres truncates
-- generated constraint names to 63 characters, and the real name was
-- `ga4_landing_pages_window_end_window_days_landing_page_chann_key` —
-- one character shorter than the guess. `DROP CONSTRAINT IF EXISTS` on a wrong
-- name succeeds silently, so 019 reported success while leaving the old
-- constraint in place.
--
-- The consequence was immediate and exactly what the domain column exists to
-- prevent: syncing the second property failed on
--
--   Key (window_end, window_days, landing_page, channel)=(2026-07-26, 28, /, Organic Search)
--
-- because "/" is a landing page on both damcogroup.com and achieva.ai. Without
-- the domain in the key, one property's homepage traffic would have silently
-- overwritten the other's.
--
-- Look the names up in the catalog rather than guessing them. Any unique
-- constraint on these tables that does not include `domain` is stale by
-- definition.
--
-- Idempotent.
-- ============================================================================

BEGIN;

DO $$
DECLARE
    r RECORD;
BEGIN
    FOR r IN
        SELECT c.conname, c.conrelid::regclass AS tbl
          FROM pg_constraint c
         WHERE c.contype = 'u'
           AND c.conrelid IN ('ga4_landing_pages'::regclass,
                              'ga4_channel_totals'::regclass)
           -- Keep any key that already accounts for the domain.
           AND NOT EXISTS (
               SELECT 1
                 FROM unnest(c.conkey) AS k
                 JOIN pg_attribute a
                   ON a.attrelid = c.conrelid AND a.attnum = k
                WHERE a.attname = 'domain'
           )
    LOOP
        EXECUTE format('ALTER TABLE %s DROP CONSTRAINT %I', r.tbl, r.conname);
        RAISE NOTICE 'Dropped stale unique constraint %I on %s', r.conname, r.tbl;
    END LOOP;
END
$$;

-- The domain-aware replacements were created as indexes in 019 and are already
-- present; assert rather than recreate, so a missing one is loud.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_indexes
                    WHERE indexname = 'uq_ga4_lp_domain_window') THEN
        RAISE EXCEPTION 'uq_ga4_lp_domain_window is missing — re-apply migration 019';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_indexes
                    WHERE indexname = 'uq_ga4_ch_domain_window') THEN
        RAISE EXCEPTION 'uq_ga4_ch_domain_window is missing — re-apply migration 019';
    END IF;
END
$$;

COMMIT;
