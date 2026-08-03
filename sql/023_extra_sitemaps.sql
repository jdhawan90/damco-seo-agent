-- ============================================================================
-- Additional sitemaps per domain (migration 023)
--
-- The problem
-- -----------
-- tenant_domains.sitemap_url holds ONE sitemap per domain, and everything
-- downstream assumes the root sitemap index reaches every page. On this tenant
-- it does not.
--
-- https://www.damcogroup.com/sitemap.xml is an All in One SEO index listing six
-- children — addl, post, page, client-success, insight, author — totalling
-- 1,151 URLs. Not one of them is under /insurance/.
--
-- Meanwhile https://www.damcogroup.com/insurance/sitemap.xml exists, returns
-- 200, and lists 47 URLs: InsuraCRM, InsureEdge, BrokerEdge, claims-management,
-- underwriting, the whole product section. Nothing references it from the root
-- index, so no crawler following the declared sitemap will ever see it. It is a
-- separate WordPress multisite with its own generator.
--
-- Why it mattered more than a missing tile
-- ----------------------------------------
-- `pages` is built FROM the sitemap. A page absent from the sitemap is absent
-- from `pages`, and therefore invisible to every agent that reads it: the site
-- auditor never audits it, cwv_monitor never measures it, internal_link_analyzer
-- never counts its links, and ga4_sync cannot attribute its conversions.
--
-- 314 of the 325 tracked Insurance keywords point at pages in that section.
-- Insurance is the second-largest offering and the second-best performing
-- (79% of its keywords rank). The entire section has been outside the system's
-- inventory the whole time, and every technical audit silently skipped it.
--
-- Insurance is the only offering affected — checked against all fifteen.
--
-- Idempotent.
-- ============================================================================

BEGIN;

ALTER TABLE tenant_domains
    ADD COLUMN IF NOT EXISTS extra_sitemaps TEXT[] NOT NULL DEFAULT '{}';

COMMENT ON COLUMN tenant_domains.extra_sitemaps IS
    'Sitemaps that the root sitemap index does not reference. WordPress '
    'multisite sections generate their own and do not register them upstream, '
    'so they are unreachable by following sitemap_url alone.';

UPDATE tenant_domains
   SET extra_sitemaps = ARRAY['https://www.damcogroup.com/insurance/sitemap.xml']
 WHERE tenant_id = active_tenant()
   AND domain = 'damcogroup.com'
   AND NOT ('https://www.damcogroup.com/insurance/sitemap.xml' = ANY(extra_sitemaps));

COMMIT;
