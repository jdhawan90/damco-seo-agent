-- ============================================================================
-- Second pass at pages.offering (migration 024)
--
-- Migration 021 ran this same mapping when `pages` held 1,404 rows. Migration
-- 023 then added the /insurance/ sitemap, and sitemap_validator brought the
-- table to 1,642 — including 47 insurance product pages that 021 could not
-- have seen. Re-running places them.
--
-- Same source of truth, same guard
-- -------------------------------
-- keywords.target_url + keywords.offering: a mapping an SEO executive made by
-- hand. Better evidence than anything inferable from a URL, and the reason this
-- is a deterministic backfill rather than a classifier.
--
-- Refuses to run if any page maps to two offerings, rather than picking one.
-- Only writes NULLs, so a human's correction is never overwritten.
--
-- What this does NOT do
-- ---------------------
-- 175 pages are typed `service` and carry no offering. Only 34 of them have a
-- keyword pointing at them, so 141 remain unassigned after this runs.
--
-- Assigning those by matching offering tokens against the URL path was tried
-- and rejected. Even with whole-word matching it produced /monday-com-consulting
-- -> vCTO (token 'consulting'), /web-analytics-services -> Cloud ('web'),
-- /our-offerings/ai-consulting-services2 -> vCTO rather than AI, and
-- /services/ibm/thank-you.html -> AS400 ('ibm'). Generic tokens — consulting,
-- development, software, web, google — match the wrong offering more often than
-- the right one, and there is no way to tell good from bad without reading the
-- page.
--
-- A wrong offering is worse than NULL. NULL reports itself honestly as
-- unattributed; a wrong label silently credits Monday.com integrations to vCTO
-- and nobody notices. Those 141 need a human or a content-reading classifier,
-- and are exported for review instead.
--
-- Blogs are deliberately excluded from any assignment: 1,055 of them, and
-- "AI in loan servicing" is legitimately both AI and Insurance. Blog
-- performance is reported by content type instead.
--
-- Idempotent.
-- ============================================================================

BEGIN;

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
           AND starts_with(k.target_url, 'http')
         GROUP BY p.id
        HAVING count(DISTINCT k.offering) > 1
    ) t;

    IF ambiguous > 0 THEN
        RAISE EXCEPTION
            '% page(s) map to more than one offering via keywords.target_url. '
            'Resolve those keyword assignments first — picking one arbitrarily '
            'would credit another offering''s traffic.', ambiguous;
    END IF;
END
$$;

WITH mapped AS (
    SELECT p.id AS page_id, min(k.offering) AS offering
      FROM pages p
      JOIN keywords k
        ON lower(rtrim(k.target_url, '/')) = lower(rtrim(p.url, '/'))
     WHERE k.offering IS NOT NULL
       AND starts_with(k.target_url, 'http')
     GROUP BY p.id
)
UPDATE pages p
   SET offering   = m.offering,
       updated_at = now()
  FROM mapped m
 WHERE p.id = m.page_id
   AND p.offering IS NULL;

COMMIT;
