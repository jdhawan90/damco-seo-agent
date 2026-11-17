-- ============================================================================
-- Pages offering priors: domain-level editorial intent         (migration 028)
--
-- Why
-- ---
-- The offering classifier's first pass left 115 blogs ambiguous and 102
-- as no_match out of 1,055. Most of the ambiguous set lived on
-- damcodigital.com — where the blogs are ABOUT client verticals
-- (insurance, healthcare, real estate) but what Damco SELLS on that
-- property is Marketing Services. Pure token scoring picks the vertical;
-- domain editorial intent says Marketing Services.
--
-- What this policy does
-- ---------------------
-- `pages_offering_priors.domain_boosts` is a {domain: {offering: score}}
-- map. Every blog whose URL host matches a keyed domain gets that
-- offering's score bumped by the configured amount BEFORE the final
-- decision. The classifier applies it per decision; the audit log
-- captures the bump so the reasoning is still reproducible.
--
-- Chosen values
-- -------------
-- damcodigital.com → Marketing Services +15 (equal to 5 URL tokens).
-- Strong enough to flip the typical cross-vertical case
-- ("Digital Marketing for Insurance": Insurance 14 vs MS 7+15=22 →
-- Marketing Services wins), not strong enough to overwhelm a
-- genuinely non-marketing post ("AI Fundamentals": AI 20 vs MS 0+15=15
-- → AI wins).
--
-- achieva.ai → Salesforce +3. Most achieva blogs already classify
-- cleanly as Salesforce (254/~300). A small nudge catches the tail
-- cases where a Salesforce blog also touches AI or DevOps and the
-- token scores run close.
--
-- Idempotent — the INSERT uses ON CONFLICT so re-running the migration
-- or re-tuning the values from code is safe.
-- ============================================================================

BEGIN;

INSERT INTO tenant_policies (tenant_id, key, value, description)
VALUES (
    active_tenant(),
    'pages_offering_priors',
    '{
        "domain_boosts": {
            "damcodigital.com": {"Marketing Services": 15},
            "achieva.ai":       {"Salesforce": 3}
        }
    }'::jsonb,
    'Additive score bonus applied by content_operations.pages_backfiller '
    'when scoring a page URL on each domain. Lets editorial intent override '
    'pure-token scoring on properties with a single dominant offering.'
)
ON CONFLICT (tenant_id, key) DO UPDATE SET
    value       = EXCLUDED.value,
    description = EXCLUDED.description,
    updated_at  = CURRENT_TIMESTAMP;

COMMIT;
