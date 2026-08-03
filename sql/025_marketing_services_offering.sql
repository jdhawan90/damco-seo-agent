-- ============================================================================
-- Add the Marketing Services offering (migration 025)
--
-- Why
-- ---
-- The fifteen offerings were modelled on damcogroup.com, which sells IT
-- services. damcodigital.com sells digital marketing, and its service pages had
-- nowhere to go: reviewing 299 service pages, 30 damcodigital.com pages were
-- marked 'Marketing Services', a service line the profile did not model.
--
--   /industry/insurance-digital-marketing
--   /industry/saas-digital-marketing
--   /industry/real-estate-digital-marketing
--   /industry/ecommerce-digital-marketing-agency
--   /industry/home-services-marketing/{hvac,roofing,solar,tree}
--   ...
--
-- Those pages exist, rank, and take traffic. Filing them under an IT offering
-- would have been wrong, and leaving them NULL made a whole property invisible
-- to per-offering reporting.
--
-- What adding an offering affects
-- -------------------------------
-- `offerings` is not just a label. It drives trend_scout's keyword
-- classification, the per-offering grouping in reports and on the dashboard,
-- and niche-token matching in platform_finder. A new offering starts empty:
-- no keywords are assigned to it, so it appears with zero coverage until
-- keywords are classified into it. That is honest — it reflects that no
-- marketing keywords are currently tracked.
--
-- sort_order 160 puts it after the existing fifteen (10..150) rather than
-- renumbering them, which would reshuffle every existing report.
--
-- Tokens are deliberately marketing-specific and avoid words the IT offerings
-- already claim. 'seo' and 'ppc' rather than 'digital' or 'services', because a
-- token shared with another offering is dropped from matching as ambiguous.
--
-- Idempotent.
-- ============================================================================

BEGIN;

INSERT INTO tenant_offerings (tenant_id, name, slug, tokens, niche_tokens,
                              sort_order, status)
SELECT active_tenant(),
       'Marketing Services',
       'marketing-services',
       ARRAY['seo', 'search engine optimization', 'ppc', 'paid search',
             'google ads', 'social media marketing', 'content marketing',
             'email marketing', 'conversion rate optimization', 'link building',
             'local seo', 'digital marketing agency', 'marketing automation',
             'brand strategy', 'media buying', 'influencer marketing',
             'lead generation', 'web analytics', 'ui ux design'],
       ARRAY['seo', 'ppc', 'marketing', 'ads', 'social', 'brand'],
       160,
       'active'
 WHERE NOT EXISTS (
     SELECT 1 FROM tenant_offerings
      WHERE tenant_id = active_tenant() AND name = 'Marketing Services');

COMMIT;
