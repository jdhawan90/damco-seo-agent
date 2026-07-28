-- ============================================================================
-- Populate pages.offering (migration 021)
--
-- The missing link between ranking and revenue.
--
-- `pages.offering` has existed since migration 001 and was NULL for all 1,404
-- rows — nothing ever wrote it. `sitemap_validator` writes url and page_type;
-- no agent sets the offering. So the moment GA4 data landed, the
-- v_ga4_offering_performance view returned zero rows: 439 landing pages
-- matched to `pages`, and not one of them knew which service line it belonged
-- to. Rank data is per-offering, behaviour data is per-page, and there was no
-- join between them.
--
-- Source of truth
-- ---------------
-- `keywords.target_url` — the page an SEO executive assigned a keyword to,
-- together with `keywords.offering`. That mapping is human-curated, which makes
-- it better evidence than anything inferable from a URL path.
--
-- Safety
-- ------
-- Verified before writing: of the 142 pages this matches, **zero** map to more
-- than one offering. The backfill is therefore deterministic, not a guess. If
-- that ever stops being true the DO block below refuses to run rather than
-- picking one arbitrarily.
--
-- Coverage is deliberately partial: 142 of 1,404 pages, 14 of 15 offerings.
-- Salesforce is absent because its keywords have no target_url assigned — a
-- known gap, and one this migration surfaces rather than papers over. Blog and
-- resource pages are mostly unmapped because no keyword targets them directly.
--
-- Only NULLs are written. A human's correction is never overwritten.
--
-- Idempotent.
-- ============================================================================

BEGIN;

-- Refuse rather than guess if the mapping has become ambiguous.
DO $$
DECLARE
    ambiguous INTEGER;
BEGIN
    SELECT count(*) INTO ambiguous FROM (
        SELECT p.id
          FROM pages p
          JOIN keywords k
            ON lower(rtrim(k.target_url, '/')) = lower(rtrim(p.url, '/'))
         WHERE k.offering IS NOT NULL
         GROUP BY p.id
        HAVING count(DISTINCT k.offering) > 1
    ) t;

    IF ambiguous > 0 THEN
        RAISE EXCEPTION
            '% page(s) map to more than one offering via keywords.target_url. '
            'Resolve those assignments before backfilling — picking one '
            'arbitrarily would attribute another offering''s traffic.', ambiguous;
    END IF;
END
$$;

WITH mapped AS (
    SELECT p.id AS page_id,
           min(k.offering) AS offering    -- min() is safe: uniqueness asserted above
      FROM pages p
      JOIN keywords k
        ON lower(rtrim(k.target_url, '/')) = lower(rtrim(p.url, '/'))
     WHERE k.offering IS NOT NULL
     GROUP BY p.id
)
UPDATE pages p
   SET offering   = m.offering,
       updated_at = now()
  FROM mapped m
 WHERE p.id = m.page_id
   AND p.offering IS NULL;      -- never overwrite a human's correction

COMMIT;
