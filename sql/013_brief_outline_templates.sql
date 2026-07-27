-- ============================================================================
-- Brief outline templates (migration 013)
--
-- Purpose
-- -------
-- `content_operations.brief_generator.template_h2_sections()` hardcoded three
-- heading skeletons — one per audience stage. They are the outline that
-- actually ships today, because LLM enrichment falls back to them whenever
-- Anthropic credit is unavailable, so they are not a throwaway default.
--
-- They are also unmistakably one company's voice: "Our methodology",
-- "Industries we serve", "Pricing and engagement models" assume a B2B firm
-- selling delivery services. A publisher, a SaaS product, or an e-commerce
-- brand would want none of those headings, and could not change them without
-- editing Python.
--
-- Behaviour is unchanged: the strings seeded below are byte-identical to the
-- ones the function returned before this migration, `{primary_kw}` included.
-- The code still does the f-string interpolation — the database stores the
-- template, not the rendered text.
--
-- Shape
-- -----
--   {"<stage>": ["<heading>", ...], ...}
--
-- Keys are the audience stages classify_audience_stage() emits: awareness,
-- consideration, decision. `consideration` doubles as the fallback for any
-- stage not present, matching the function's existing default branch.
-- `{primary_kw}` is the only placeholder recognised; a heading without it is
-- emitted verbatim.
--
-- Idempotent. Safe to re-run — ON CONFLICT DO NOTHING, so an operator who has
-- since edited the row keeps their edit.
-- ============================================================================

BEGIN;

INSERT INTO tenant_policies (tenant_id, key, value, description) VALUES
    (active_tenant(), 'brief_outline_templates',
     '{
        "awareness": [
          "What is {primary_kw}?",
          "Why {primary_kw} matters",
          "How {primary_kw} works (overview)",
          "Common use cases",
          "Key terms and concepts",
          "Next steps for businesses considering {primary_kw}"
        ],
        "consideration": [
          "Our {primary_kw} capabilities",
          "How we approach {primary_kw}",
          "Use cases and outcomes",
          "Tech stack and integrations",
          "Industries we serve",
          "FAQ"
        ],
        "decision": [
          "What our {primary_kw} engagement looks like",
          "Industries we serve",
          "Our methodology",
          "Case studies and proof points",
          "Pricing and engagement models",
          "FAQ"
        ]
      }'::jsonb,
     'H2 skeletons per audience stage for content briefs. The seeded values '
     'are a B2B-services seller''s voice ("Our methodology", "Pricing and '
     'engagement models") — a different vertical should rewrite them rather '
     'than inherit them. {primary_kw} is interpolated by brief_generator; '
     '"consideration" is the fallback for unrecognised stages.')
ON CONFLICT (tenant_id, key) DO NOTHING;

COMMIT;
