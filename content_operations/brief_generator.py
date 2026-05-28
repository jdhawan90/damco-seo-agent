"""
Brief Generator — Phase 2 module of Content Operations
=======================================================

Takes a target (a keyword cluster, a coverage gap, or a specific URL)
and produces a complete SEO content brief — the document a writer
needs to draft an actual ranking page.

Designed to chain off `competitive_intelligence.gap_analyzer`:
gap_analyzer says "we have 347 coverage gaps where competitors rank top
10 and we don't", brief_generator picks the highest-demand ones and
emits writeable briefs.

What's in a brief
-----------------
For each target:
  1. Primary + secondary keywords (with GSC stats: clicks, impressions, position)
  2. Suggested target URL (slug derived from primary keyword)
  3. Audience stage classification (awareness / consideration / decision) —
     rule-based heuristic based on keyword wording
  4. Top 5 competitor reference URLs from the SERP we want to win
  5. Recommended heading outline (LLM-extends rule-based template using
     competitor titles as inspiration)
  6. Internal linking suggestions (existing Damco pages in same offering)
  7. AEO checklist (hardcoded — always present per the agent's safety rule)
  8. LLM-generated narrative sections (intro hook, topic angle, unique POV)
  9. Recommended word count (based on page_type + competitor avg)

Outputs
-------
- content_briefs row (DB): brief_content (JSONB), target_url, status='draft'
- outputs/briefs/<slug>_<date>.md: writable markdown brief

LLM behavior
------------
Uses common.llm.call_claude with tier='default' (Sonnet).
- When ANTHROPIC_API_KEY + credit are available: full LLM output for the
  narrative + heading sections
- When LLM is unavailable: rule-based skeleton briefs still generate
  (clearly labeled with [PLACEHOLDER — load Anthropic credit] tags so
  writers know to revisit those sections)

Cost per brief: ~$0.02-0.05 with Sonnet. 30-brief coverage-gap batch
runs ~$0.60-1.50.

Usage
-----
    # Pick the top 10 coverage-gap keywords from gap_analyzer logic and
    # generate briefs for each (most common use)
    python -m content_operations.brief_generator --coverage-gap --limit 10

    # Restrict to one offering
    python -m content_operations.brief_generator --coverage-gap --offering "AI" --limit 5

    # Manual: brief for a specific keyword cluster
    python -m content_operations.brief_generator --keyword-ids 42,43,45

    # Skip LLM (force rule-based output even if credit is available)
    python -m content_operations.brief_generator --coverage-gap --limit 5 --no-llm

    # Dry run — generate briefs, write to disk, but don't insert
    # content_briefs DB rows
    python -m content_operations.brief_generator --coverage-gap --limit 3 --dry-run
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from common.database import connection, fetch_all, record_agent_run
from common.llm import call_claude, LLMUnavailableError


logger = logging.getLogger("brief_generator")
AGENT_NAME = "content_operations.brief_generator"

DEFAULT_LIMIT = 10                # default # of briefs per coverage-gap run
RECOMMENDED_WORD_COUNT = {        # by page_type (matches site_auditor thresholds)
    "home":     200,
    "pillar":   1500,
    "service":  1000,
    "blog":      800,
    "resource":  500,
    "landing":   400,
}
DEFAULT_WORD_COUNT = 800

OUTPUT_DIR = Path(__file__).resolve().parent.parent / "outputs" / "briefs"

# AEO (Answer Engine Optimization) checklist — hardcoded per the agent's
# safety rule: "Include the AEO checklist in every brief."
AEO_CHECKLIST = [
    "Does the content answer one crisp, extractable question in the first 200 words?",
    "Is there a 'Key facts' or definitions section with bullet-able stats that AI search can quote?",
    "Are headings phrased as questions where natural? (helps PAA + AI Overview eligibility)",
    "Are bulleted/numbered lists used for scannable, citable content?",
    "Is the author or page owner identified with credentials?",
    "Are external sources cited inline (URLs in the body) so AI engines can verify?",
    "Are FAQs included with structured answers (3-6 Q&A pairs at minimum)?",
    "Is FAQPage schema markup applied to the FAQ section?",
    "Is the primary keyword in the title, H1, first 100 words, and meta description?",
]


# ---------------------------------------------------------------------------
# Read phase
# ---------------------------------------------------------------------------

def load_keyword_with_context(keyword_id: int) -> dict | None:
    """Pull a keyword + its latest Damco position + GSC stats + top-5 competitors."""
    rows = fetch_all(
        """
        WITH latest_serp AS (
            SELECT keyword_id, max(date) AS d FROM keyword_rankings
             WHERE source = 'dataforseo' GROUP BY keyword_id
        ),
        latest_gsc AS (
            SELECT keyword_id, max(date) AS d FROM keyword_rankings
             WHERE source = 'gsc' GROUP BY keyword_id
        )
        SELECT k.id, k.keyword, k.offering, k.target_url, k.intent, k.journey_stage,
               serp.rank_position  AS damco_position,
               serp.url_found      AS damco_url,
               gsc.rank_position   AS gsc_position,
               gsc.clicks          AS gsc_clicks,
               gsc.impressions     AS gsc_impressions
          FROM keywords k
     LEFT JOIN latest_serp ls ON ls.keyword_id = k.id
     LEFT JOIN keyword_rankings serp ON serp.keyword_id = k.id AND serp.date = ls.d AND serp.source = 'dataforseo'
     LEFT JOIN latest_gsc lg ON lg.keyword_id = k.id
     LEFT JOIN keyword_rankings gsc ON gsc.keyword_id = k.id AND gsc.date = lg.d AND gsc.source = 'gsc'
         WHERE k.id = %s
        """,
        [keyword_id],
    )
    if not rows:
        return None
    k = rows[0]
    k["competitors"] = load_top_competitors(keyword_id, n=5)
    return k


def load_top_competitors(keyword_id: int, n: int = 5) -> list[dict]:
    """Top N competitor URLs (by rank) for this keyword's latest snapshot."""
    return fetch_all(
        """
        SELECT cr.rank_position, c.competitor_domain, c.category, c.threat_tier,
               cr.url_found, cr.url_title, cr.page_type
          FROM competitor_rankings cr
          JOIN competitors c ON c.id = cr.competitor_id
         WHERE cr.keyword_id = %s
           AND cr.date = (SELECT max(date) FROM competitor_rankings WHERE keyword_id = %s)
           AND cr.rank_position BETWEEN 1 AND %s
         ORDER BY cr.rank_position
        """,
        [keyword_id, keyword_id, n],
    )


def load_coverage_gap_keywords(offering: str | None, limit: int) -> list[int]:
    """
    Mirrors gap_analyzer.classify(): keywords where Damco isn't in the top 100
    and at least one tracked competitor IS in the top 10. Ranked by:
        GSC impressions desc (real demand)
        then count of top-10 tracked competitors desc
        then keyword alphabetical
    Returns the keyword_ids of the top `limit` candidates.
    """
    params: list = [limit]
    sql = """
        WITH latest_serp AS (
            SELECT keyword_id, max(date) AS d FROM keyword_rankings
             WHERE source = 'dataforseo' GROUP BY keyword_id
        ),
        latest_gsc AS (
            SELECT keyword_id, max(date) AS d FROM keyword_rankings
             WHERE source = 'gsc' GROUP BY keyword_id
        ),
        latest_comp AS (
            SELECT keyword_id, max(date) AS d FROM competitor_rankings
             GROUP BY keyword_id
        ),
        coverage_gaps AS (
            SELECT k.id AS keyword_id, k.keyword,
                   coalesce(gsc.impressions, 0) AS gsc_impressions,
                   coalesce(gsc.clicks, 0)      AS gsc_clicks,
                   (SELECT count(*)
                      FROM competitor_rankings cr
                      JOIN latest_comp lc ON lc.keyword_id = cr.keyword_id AND lc.d = cr.date
                      JOIN competitors c ON c.id = cr.competitor_id
                     WHERE cr.keyword_id = k.id
                       AND cr.rank_position BETWEEN 1 AND 10
                       AND c.is_tracked = TRUE)        AS tracked_top10_count
              FROM keywords k
         LEFT JOIN latest_serp ls ON ls.keyword_id = k.id
         LEFT JOIN keyword_rankings serp ON serp.keyword_id = k.id AND serp.date = ls.d AND serp.source = 'dataforseo'
         LEFT JOIN latest_gsc lg ON lg.keyword_id = k.id
         LEFT JOIN keyword_rankings gsc ON gsc.keyword_id = k.id AND gsc.date = lg.d AND gsc.source = 'gsc'
             WHERE k.status = 'active'
               AND serp.rank_position IS NULL
    """
    if offering:
        sql += " AND k.offering = %s\n"
        params = [offering] + params
    sql += """
        )
        SELECT keyword_id FROM coverage_gaps
         WHERE tracked_top10_count >= 1
         ORDER BY gsc_impressions DESC, tracked_top10_count DESC, keyword
         LIMIT %s
    """
    params = params[:-1] + [limit] if offering else [limit]
    if offering:
        params = [offering, limit]
    rows = fetch_all(sql, params)
    return [r["keyword_id"] for r in rows]


def load_internal_link_targets(offering: str | None, exclude_url: str | None) -> list[dict]:
    """
    Candidate Damco pages for internal linking. We DO NOT filter by
    pages.offering because that column is sparsely populated (the
    sitemap discoverer doesn't know which offering a URL belongs to —
    that's a content-team labeling task we haven't backfilled).
    Topical relevance is decided by suggest_internal_links() via
    lexical overlap with the primary + secondary keywords.

    We do narrow the pool to pages that have been audited (title not
    NULL) so we have something to score against.
    """
    params: list = []
    sql = """
        SELECT url, title, page_type, word_count
          FROM pages
         WHERE title IS NOT NULL
           AND page_type IS NOT NULL
    """
    if exclude_url:
        sql += " AND url <> %s"
        params.append(exclude_url)
    sql += """
         ORDER BY
           CASE page_type
             WHEN 'pillar'  THEN 1
             WHEN 'service' THEN 2
             WHEN 'resource' THEN 3
             ELSE 4
           END,
           word_count DESC NULLS LAST
         LIMIT 200
    """
    return fetch_all(sql, params)


# ---------------------------------------------------------------------------
# Rule-based heuristics
# ---------------------------------------------------------------------------

def classify_audience_stage(keyword: str, intent: str | None) -> tuple[str, str]:
    """Returns (stage, rationale). stage in {awareness, consideration, decision}."""
    if intent:
        if intent.lower() == "transactional":
            return "decision", f"Marked intent={intent} in DB."
        if intent.lower() == "commercial":
            return "consideration", f"Marked intent={intent} in DB."
        if intent.lower() == "informational":
            return "awareness", f"Marked intent={intent} in DB."

    kw = (keyword or "").lower()
    # decision-stage signals
    if any(s in kw for s in ("pricing", "cost", "buy ", "hire ", "agency", "company",
                              "vendor", "partner", "consultant")):
        return "decision", "Keyword contains vendor-selection signal (company/agency/pricing/hire)."
    # awareness-stage signals
    if any(s in kw for s in ("what is", "how does", "guide", "definition", "meaning",
                              "explained", "for beginners", "introduction", "basics")):
        return "awareness", "Keyword contains learning-intent signal."
    if " vs " in kw or "alternatives to " in kw:
        return "consideration", "Comparison/alternative-research keyword."
    # consideration default — service-style keywords are typically mid-funnel
    if any(s in kw for s in ("services", "solutions", "platform", "software")):
        return "consideration", "Service-class keyword (mid-funnel evaluation)."

    return "consideration", "Default — most B2B keywords sit in consideration."


def slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s or "untitled"


def suggest_target_url(primary_keyword: str, offering: str | None) -> str:
    """Suggest a clean URL path for the new page."""
    slug = slugify(primary_keyword)
    # Trim trailing "-services" or "-company" noise if extremely long
    if len(slug) > 60:
        slug = re.sub(r"-(?:services|company|companies|consulting)$", "", slug)
    return f"/{slug}"


def derive_secondary_keywords(primary_kw_id: int, primary_kw: str,
                              offering: str | None, max_secondary: int = 8) -> list[dict]:
    """
    Find related keywords in the same offering that share lexical tokens
    with the primary. These become secondary kw to weave into the page.
    """
    if not offering:
        return []
    primary_tokens = set(re.findall(r"[a-z0-9]+", primary_kw.lower()))
    if not primary_tokens:
        return []
    # Drop the most generic single-letter / two-letter tokens
    primary_tokens = {t for t in primary_tokens if len(t) >= 3}

    rows = fetch_all(
        """
        WITH latest_gsc AS (
            SELECT keyword_id, max(date) AS d FROM keyword_rankings
             WHERE source = 'gsc' GROUP BY keyword_id
        )
        SELECT k.id, k.keyword,
               gsc.impressions AS gsc_impressions,
               gsc.clicks      AS gsc_clicks
          FROM keywords k
     LEFT JOIN latest_gsc lg ON lg.keyword_id = k.id
     LEFT JOIN keyword_rankings gsc ON gsc.keyword_id = k.id AND gsc.date = lg.d AND gsc.source = 'gsc'
         WHERE k.status = 'active' AND k.offering = %s AND k.id <> %s
         ORDER BY gsc.impressions DESC NULLS LAST, k.keyword
         LIMIT 200
        """,
        [offering, primary_kw_id],
    )

    # Score by lexical overlap
    scored: list[tuple[float, dict]] = []
    for r in rows:
        tokens = set(re.findall(r"[a-z0-9]+", (r["keyword"] or "").lower()))
        if not tokens:
            continue
        overlap = len(tokens & primary_tokens)
        if overlap < 2:
            continue
        score = overlap + (r["gsc_impressions"] or 0) / 1000.0
        scored.append((score, r))
    scored.sort(key=lambda x: -x[0])
    return [{"keyword_id": r["id"], "keyword": r["keyword"],
             "gsc_impressions": r["gsc_impressions"] or 0,
             "gsc_clicks": r["gsc_clicks"] or 0}
            for _, r in scored[:max_secondary]]


def template_h2_sections(primary_kw: str, stage: str, page_type: str | None) -> list[str]:
    """Conservative outline skeleton. LLM enhances when available."""
    if stage == "awareness":
        return [
            f"What is {primary_kw}?",
            f"Why {primary_kw} matters",
            f"How {primary_kw} works (overview)",
            "Common use cases",
            "Key terms and concepts",
            "Next steps for businesses considering {primary_kw}",
        ]
    if stage == "decision":
        return [
            f"What our {primary_kw} engagement looks like",
            "Industries we serve",
            "Our methodology",
            "Case studies and proof points",
            "Pricing and engagement models",
            "FAQ",
        ]
    # consideration default
    return [
        f"Our {primary_kw} capabilities",
        f"How we approach {primary_kw}",
        "Use cases and outcomes",
        "Tech stack and integrations",
        "Industries we serve",
        "FAQ",
    ]


# ---------------------------------------------------------------------------
# LLM enrichment
# ---------------------------------------------------------------------------

def make_llm_prompt(kw: dict, secondary: list[dict], stage: str,
                    template_outline: list[str]) -> str:
    competitors_str = "\n".join(
        f"  #{c['rank_position']} {c['competitor_domain']} -- {(c.get('url_title') or '')[:120]}  ({c['url_found']})"
        for c in kw["competitors"][:5]
    ) or "  (no competitor data)"
    secondary_str = ", ".join(s["keyword"] for s in secondary[:6]) or "(none)"
    template_str = "\n".join(f"  - {s}" for s in template_outline)

    return f"""You are writing an SEO content brief for Damco Group (B2B IT services and AI consulting). A writer will use your output verbatim to draft a new page targeting the keyword below. Be specific, opinionated, and grounded in the competitive data shown.

PRIMARY KEYWORD: {kw['keyword']!r}
OFFERING:        {kw.get('offering') or '(unspecified)'}
AUDIENCE STAGE:  {stage}
SECONDARY KEYWORDS to weave in naturally: {secondary_str}
CURRENT TOP-5 COMPETITORS for this keyword:
{competitors_str}

PROPOSED OUTLINE SKELETON (you'll refine):
{template_str}

Produce a JSON object with exactly these fields. Output ONLY the JSON, no preamble or commentary:

{{
  "intro_hook": "<2-3 sentence opening paragraph that hooks a B2B decision-maker. Use the audience stage to set tone. Mention the primary keyword once, naturally.>",
  "topic_angle": "<1-2 sentence positioning statement: what unique angle should Damco take vs the competitors listed? Be specific to those competitors.>",
  "unique_pov": "<3-5 short bullet points (as a JSON array of strings) of differentiators Damco should emphasize that the listed competitors don't. Concrete, not generic.>",
  "refined_outline": [
    "<H2 heading 1 — refined from the skeleton, more specific and search-friendly>",
    "<H2 heading 2>",
    "...6-8 headings total..."
  ],
  "must_include_topics": [
    "<concrete subtopic 1 that the page must cover to compete with the listed competitors>",
    "<concrete subtopic 2>",
    "...4-6 subtopics..."
  ],
  "questions_to_answer": [
    "<question 1 — exactly the kind a B2B buyer types into ChatGPT/Perplexity>",
    "<question 2>",
    "...4-6 questions..."
  ]
}}"""


def enrich_with_llm(kw: dict, secondary: list[dict], stage: str,
                    template_outline: list[str], allow_llm: bool) -> tuple[dict, dict | None]:
    """
    Returns (llm_block_dict, usage_dict_or_none).
    llm_block_dict has the same keys regardless of LLM availability; when LLM
    isn't available, values are populated with [PLACEHOLDER ...] strings.
    """
    placeholder = lambda label: f"[PLACEHOLDER — load Anthropic credit and re-run for {label}]"

    if not allow_llm:
        return ({
            "intro_hook":         placeholder("intro hook"),
            "topic_angle":        placeholder("topic angle"),
            "unique_pov":         [placeholder("unique POV bullets")],
            "refined_outline":    template_outline,
            "must_include_topics": [placeholder("must-include subtopics")],
            "questions_to_answer": [placeholder("buyer questions")],
            "_source":            "rule-based (LLM bypassed by --no-llm)",
        }, None)

    prompt = make_llm_prompt(kw, secondary, stage, template_outline)
    try:
        text, usage = call_claude(prompt, tier="default", max_tokens=2500, temperature=0.7)
    except LLMUnavailableError as exc:
        logger.warning("LLM unavailable, using rule-based skeleton: %s", exc)
        return ({
            "intro_hook":         placeholder("intro hook"),
            "topic_angle":        placeholder("topic angle"),
            "unique_pov":         [placeholder("unique POV bullets")],
            "refined_outline":    template_outline,
            "must_include_topics": [placeholder("must-include subtopics")],
            "questions_to_answer": [placeholder("buyer questions")],
            "_source":            f"rule-based (LLM unavailable: {exc})",
        }, None)

    # Parse JSON out of the LLM response. Be tolerant of leading/trailing text.
    block: dict = {}
    try:
        # Strip code fences if present
        cleaned = re.sub(r"^```(?:json)?\s*", "", text.strip())
        cleaned = re.sub(r"\s*```\s*$", "", cleaned)
        # Find the first { and last } — the model occasionally adds commentary
        first = cleaned.find("{")
        last  = cleaned.rfind("}")
        if first >= 0 and last > first:
            block = json.loads(cleaned[first:last + 1])
    except (json.JSONDecodeError, ValueError) as exc:
        logger.warning("Failed to parse LLM JSON: %s. Raw text snippet: %r", exc, text[:200])
        block = {
            "intro_hook":         placeholder("intro hook (LLM returned unparseable JSON)"),
            "topic_angle":        placeholder("topic angle"),
            "unique_pov":         [placeholder("unique POV bullets")],
            "refined_outline":    template_outline,
            "must_include_topics": [placeholder("must-include subtopics")],
            "questions_to_answer": [placeholder("buyer questions")],
            "_source":            "rule-based (LLM JSON parse failed)",
            "_llm_raw_first_500": text[:500],
        }

    # Normalize types — model sometimes returns strings where we expected arrays
    for key in ("unique_pov", "refined_outline", "must_include_topics", "questions_to_answer"):
        v = block.get(key)
        if isinstance(v, str):
            block[key] = [v]
        elif not isinstance(v, list):
            block[key] = [placeholder(key)]

    block.setdefault("intro_hook",  placeholder("intro hook"))
    block.setdefault("topic_angle", placeholder("topic angle"))
    block.setdefault("_source", f"LLM ({usage['model']})")
    return block, usage


# ---------------------------------------------------------------------------
# Internal link suggestions
# ---------------------------------------------------------------------------

# Tokens too generic to be useful for topical matching — they appear in
# almost every service page on a B2B site, so they're false-positive bait.
GENERIC_MATCH_TOKENS = {
    "services", "service", "solution", "solutions", "company", "companies",
    "consulting", "consultant", "consultants", "partner", "partners",
    "agency", "agencies", "vendor", "vendors",
    "development", "implementation", "integration", "support", "management",
    "provider", "providers", "team", "expert", "experts", "business",
    "online", "digital", "platform", "tools", "tool", "system", "systems",
    "best", "top", "leading", "professional",
    "for", "the", "and", "with", "from", "your", "our",
}


def suggest_internal_links(primary_kw: str, secondary: list[dict],
                           candidates: list[dict], n: int = 5) -> list[dict]:
    """
    Pick existing Damco pages most topically related to this brief's
    primary + secondary keywords. Matching ignores generic tokens
    ("services", "solutions", "company", etc.) so we don't match every
    service page on the site just because they share the word "services".
    """
    if not candidates:
        return []

    # Build the "topic" token set — words specific to this brief's theme
    raw_tokens: set[str] = set()
    raw_tokens.update(re.findall(r"[a-z0-9]+", primary_kw.lower()))
    for s in secondary:
        raw_tokens.update(re.findall(r"[a-z0-9]+", s["keyword"].lower()))
    topic_tokens = {t for t in raw_tokens if len(t) >= 4 and t not in GENERIC_MATCH_TOKENS}
    if not topic_tokens:
        return []

    scored: list[tuple[float, dict]] = []
    for c in candidates:
        text = " ".join([c.get("title") or "", c.get("url") or ""]).lower()
        text_tokens = set(re.findall(r"[a-z0-9]+", text))
        # Score on non-generic overlap only
        topical_overlap = len(topic_tokens & text_tokens)
        if topical_overlap == 0:
            continue
        # Page-type bonus
        type_bonus = {"pillar": 0.5, "service": 0.4, "resource": 0.2}.get(c.get("page_type") or "", 0)
        score = topical_overlap + type_bonus
        scored.append((score, c))
    scored.sort(key=lambda x: -x[0])

    out: list[dict] = []
    for _, c in scored[:n]:
        out.append({
            "url":     c["url"],
            "title":   c.get("title"),
            "page_type": c.get("page_type"),
            "anchor_suggestion": (
                c.get("title") or c["url"].rstrip("/").split("/")[-1].replace("-", " ")
            ).strip(),
        })
    return out


# ---------------------------------------------------------------------------
# Brief assembly
# ---------------------------------------------------------------------------

def build_brief(kw: dict, allow_llm: bool) -> tuple[dict, dict | None]:
    """Assemble the full brief dict for a primary keyword. Returns (brief, llm_usage)."""
    offering = kw.get("offering")
    secondary = derive_secondary_keywords(kw["id"], kw["keyword"], offering)
    stage, stage_rationale = classify_audience_stage(kw["keyword"], kw.get("intent"))

    target_url_suggestion = suggest_target_url(kw["keyword"], offering)
    page_type = "service"   # default for B2B service pages; could be inferred per keyword

    template_outline = template_h2_sections(kw["keyword"], stage, page_type)
    llm_block, usage = enrich_with_llm(kw, secondary, stage, template_outline, allow_llm)

    link_candidates = load_internal_link_targets(offering, exclude_url=None)
    internal_links = suggest_internal_links(kw["keyword"], secondary, link_candidates, n=5)

    recommended_wc = RECOMMENDED_WORD_COUNT.get(page_type, DEFAULT_WORD_COUNT)

    return ({
        "schema_version": "1.0",
        "generated_at":   datetime.now(timezone.utc).isoformat(),
        "generated_by":   AGENT_NAME,
        "target": {
            "primary_keyword":         kw["keyword"],
            "primary_keyword_id":      kw["id"],
            "secondary_keywords":      secondary,
            "target_url_suggestion":   target_url_suggestion,
            "page_type":               page_type,
            "offering":                offering,
        },
        "demand": {
            "primary_kw_gsc_position":    float(kw["gsc_position"]) if kw.get("gsc_position") else None,
            "primary_kw_gsc_clicks_14d":  kw.get("gsc_clicks") or 0,
            "primary_kw_gsc_impr_14d":    kw.get("gsc_impressions") or 0,
            "current_damco_position":    kw.get("damco_position"),
        },
        "audience": {
            "stage":     stage,
            "rationale": stage_rationale,
        },
        "competitors": [
            {
                "rank":     c["rank_position"],
                "domain":   c["competitor_domain"],
                "category": c.get("category"),
                "threat":   c.get("threat_tier"),
                "url":      c.get("url_found"),
                "title":    c.get("url_title"),
                "page_type": c.get("page_type"),
            }
            for c in kw["competitors"]
        ],
        "outline": {
            "h1_suggestion":   kw["keyword"].title(),
            "h2_sections":     llm_block.get("refined_outline") or template_outline,
            "must_include_topics": llm_block.get("must_include_topics", []),
            "questions_to_answer": llm_block.get("questions_to_answer", []),
        },
        "narrative": {
            "intro_hook":  llm_block.get("intro_hook"),
            "topic_angle": llm_block.get("topic_angle"),
            "unique_pov":  llm_block.get("unique_pov", []),
            "_source":     llm_block.get("_source"),
        },
        "internal_links_suggested": internal_links,
        "aeo_checklist":            list(AEO_CHECKLIST),
        "recommended_word_count":   recommended_wc,
        "writer_notes": (
            "Replace any [PLACEHOLDER ...] strings before delivery. "
            "Run content_operations.compliance_checker once the draft URL is live."
        ),
    }, usage)


# ---------------------------------------------------------------------------
# Output writers
# ---------------------------------------------------------------------------

def write_brief_markdown(brief: dict) -> Path:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    primary = brief["target"]["primary_keyword"]
    slug    = slugify(primary)[:60]
    path = OUTPUT_DIR / f"{slug}_{date.today().isoformat()}.md"

    p: list[str] = []
    p.append(f"# Brief: {primary}")
    p.append("")
    p.append(f"_Generated {brief['generated_at']} by `{brief['generated_by']}`._")
    p.append("")
    p.append("## At a glance")
    p.append("")
    p.append(f"- **Primary keyword:** `{primary}`")
    p.append(f"- **Suggested URL:** `{brief['target']['target_url_suggestion']}`")
    p.append(f"- **Page type:** {brief['target']['page_type']}")
    p.append(f"- **Offering:** {brief['target']['offering'] or '—'}")
    p.append(f"- **Audience stage:** {brief['audience']['stage']} — _{brief['audience']['rationale']}_")
    p.append(f"- **Recommended word count:** {brief['recommended_word_count']}+")
    d = brief["demand"]
    if d.get("primary_kw_gsc_impr_14d"):
        p.append(f"- **GSC demand (14d):** {d['primary_kw_gsc_impr_14d']} impressions, "
                 f"{d['primary_kw_gsc_clicks_14d']} clicks, "
                 f"avg position {d['primary_kw_gsc_position'] or '—'}")
    else:
        p.append("- **GSC demand:** (no GSC data yet — this term may be new)")
    p.append(f"- **Current Damco position:** {d.get('current_damco_position') or 'not in top 100'}")
    p.append("")

    p.append("## Suggested H1")
    p.append("")
    p.append(f"`{brief['outline']['h1_suggestion']}`")
    p.append("")

    p.append("## Secondary keywords to weave in")
    p.append("")
    if not brief["target"]["secondary_keywords"]:
        p.append("_(none derived — primary keyword is too distinct from its offering's other terms)_")
    else:
        p.append("| Keyword | GSC Impr | GSC Clicks |")
        p.append("|---|---:|---:|")
        for s in brief["target"]["secondary_keywords"]:
            p.append(f"| `{s['keyword']}` | {s['gsc_impressions']} | {s['gsc_clicks']} |")
    p.append("")

    p.append("## Heading outline")
    p.append("")
    for h in brief["outline"]["h2_sections"]:
        p.append(f"- `H2:` {h}")
    p.append("")

    if brief["outline"].get("must_include_topics"):
        p.append("### Must-include subtopics")
        p.append("")
        for t in brief["outline"]["must_include_topics"]:
            p.append(f"- {t}")
        p.append("")

    if brief["outline"].get("questions_to_answer"):
        p.append("### Questions the page must answer (also use as FAQ candidates)")
        p.append("")
        for q in brief["outline"]["questions_to_answer"]:
            p.append(f"- {q}")
        p.append("")

    p.append("## Narrative angle (use for opening + framing)")
    p.append("")
    p.append(f"_Source: {brief['narrative']['_source']}_")
    p.append("")
    p.append("**Intro hook (drop this in the first 200 words, then expand):**")
    p.append("")
    p.append(f"> {brief['narrative']['intro_hook']}")
    p.append("")
    p.append("**Topic angle / unique positioning:**")
    p.append("")
    p.append(f"> {brief['narrative']['topic_angle']}")
    p.append("")
    p.append("**Unique POV — what to emphasize that the listed competitors don't:**")
    p.append("")
    for pov in brief["narrative"]["unique_pov"]:
        p.append(f"- {pov}")
    p.append("")

    p.append("## Competitor reference URLs (the SERP we need to outrank)")
    p.append("")
    if not brief["competitors"]:
        p.append("_(no competitor data yet — run keyword_intelligence.rank_tracker on this kw first)_")
    else:
        p.append("| # | Domain | Threat | Category | URL | Title |")
        p.append("|---:|---|---|---|---|---|")
        for c in brief["competitors"]:
            t = (c.get("title") or "")[:80]
            p.append(f"| {c['rank']} | `{c['domain']}` | {c.get('threat') or '?'} | "
                     f"{c.get('category') or '?'} | `{c.get('url') or ''}` | {t} |")
    p.append("")

    p.append("## Internal linking — suggested anchors / target pages")
    p.append("")
    if not brief["internal_links_suggested"]:
        p.append("_(no related Damco pages found — consider also linking from a relevant pillar after publish)_")
    else:
        p.append("| Target page | Page type | Anchor suggestion |")
        p.append("|---|---|---|")
        for il in brief["internal_links_suggested"]:
            p.append(f"| `{il['url']}` | {il['page_type'] or '?'} | _{il['anchor_suggestion']}_ |")
    p.append("")

    p.append("## AEO checklist (must pass before publish)")
    p.append("")
    for item in brief["aeo_checklist"]:
        p.append(f"- [ ] {item}")
    p.append("")

    p.append("---")
    p.append(f"_Writer notes:_ {brief['writer_notes']}")

    path.write_text("\n".join(p), encoding="utf-8")
    return path


def insert_content_brief(brief: dict, file_path: Path) -> int:
    """Persist the brief to the DB. Returns content_briefs.id."""
    target_url = brief["target"]["target_url_suggestion"]
    keywords_json = json.dumps(
        [brief["target"]["primary_keyword_id"]]
        + [s["keyword_id"] for s in brief["target"]["secondary_keywords"]]
    )
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO content_briefs
                    (page_id, target_url, keywords_json, brief_content,
                     file_path, status, assigned_writer)
                VALUES (NULL, %s, %s::jsonb, %s::jsonb, %s, 'draft', NULL)
                RETURNING id
                """,
                (target_url, keywords_json, json.dumps(brief), str(file_path)),
            )
            return cur.fetchone()[0]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def resolve_target_keyword_ids(coverage_gap: bool, keyword_ids: list[int] | None,
                                offering: str | None, limit: int) -> list[int]:
    if keyword_ids:
        return keyword_ids
    if coverage_gap:
        return load_coverage_gap_keywords(offering, limit)
    return []


def run(coverage_gap: bool = False,
        keyword_ids: list[int] | None = None,
        offering: str | None = None,
        limit: int = DEFAULT_LIMIT,
        allow_llm: bool = True,
        dry_run: bool = False) -> dict:
    start = time.monotonic()
    target_ids = resolve_target_keyword_ids(coverage_gap, keyword_ids, offering, limit)
    if not target_ids:
        logger.warning("No target keywords resolved. Use --coverage-gap or --keyword-ids.")
        return {"status": "skipped", "reason": "no targets"}

    logger.info("Generating %d brief(s)  [LLM=%s]", len(target_ids), "ON" if allow_llm else "OFF")

    briefs_written: list[dict] = []
    total_llm_in = total_llm_out = 0
    total_llm_cost = 0.0
    llm_used_count = 0
    errors: list[str] = []

    for i, kid in enumerate(target_ids, 1):
        kw = load_keyword_with_context(kid)
        if not kw:
            errors.append(f"keyword_id={kid} not found")
            continue
        logger.info("[%d/%d] %s (id=%d)", i, len(target_ids), kw["keyword"], kid)
        try:
            brief, usage = build_brief(kw, allow_llm=allow_llm)
        except Exception as exc:
            logger.error("Failed to build brief for %s: %s", kw["keyword"], exc)
            errors.append(f"{kw['keyword']}: {exc}")
            continue

        md_path = write_brief_markdown(brief)

        if dry_run:
            briefs_written.append({"keyword": kw["keyword"], "md_path": str(md_path),
                                   "content_brief_id": None})
        else:
            try:
                brief_id = insert_content_brief(brief, md_path)
                briefs_written.append({
                    "keyword": kw["keyword"], "md_path": str(md_path),
                    "content_brief_id": brief_id,
                })
            except Exception as exc:
                logger.error("Failed to insert content_brief for %s: %s", kw["keyword"], exc)
                errors.append(f"{kw['keyword']}: DB insert failed - {exc}")

        if usage:
            llm_used_count += 1
            total_llm_in   += usage["input_tokens"]
            total_llm_out  += usage["output_tokens"]
            total_llm_cost += usage["est_cost_usd"]

    duration = time.monotonic() - start

    if not dry_run:
        record_agent_run(
            agent_name=AGENT_NAME,
            status="success" if not errors else "partial",
            records_processed=len(briefs_written),
            errors=errors[:25],
            duration_seconds=round(duration, 2),
            metadata={
                "run_date":           date.today().isoformat(),
                "coverage_gap_mode":  coverage_gap,
                "offering_filter":    offering,
                "requested_limit":    limit,
                "allow_llm":          allow_llm,
                "targets_resolved":   len(target_ids),
                "briefs_written":     len(briefs_written),
                "errors":             len(errors),
                "llm_used_count":     llm_used_count,
                "llm_input_tokens":   total_llm_in,
                "llm_output_tokens":  total_llm_out,
                "llm_est_cost_usd":   round(total_llm_cost, 4),
            },
        )

    print()
    print(f"  {'=' * 72}")
    print(f"   BRIEF GENERATOR -- {date.today().isoformat()}")
    print(f"  {'=' * 72}")
    print()
    print(f"  Targets resolved:     {len(target_ids)}")
    print(f"  Briefs written:       {len(briefs_written)}")
    print(f"  Errors:               {len(errors)}")
    if allow_llm:
        print(f"  LLM-enriched briefs:  {llm_used_count}/{len(briefs_written)}")
        if total_llm_cost > 0:
            print(f"  LLM cost:             ~${total_llm_cost:.4f}  "
                  f"({total_llm_in} in / {total_llm_out} out)")
    else:
        print(f"  LLM:                  bypassed (--no-llm)")
    print(f"  Output dir:           {OUTPUT_DIR}")
    print(f"  Duration:             {duration:.1f}s")
    if briefs_written:
        print()
        print(f"  Briefs created (first 5):")
        for b in briefs_written[:5]:
            cb = f"#{b['content_brief_id']}" if b.get("content_brief_id") else "(dry-run)"
            print(f"    {cb:>6}  {b['md_path']}")
    print()

    return {
        "status":            "success" if not errors else "partial",
        "briefs_written":    len(briefs_written),
        "errors":            len(errors),
        "llm_cost_usd":      round(total_llm_cost, 4),
        "duration_seconds":  round(duration, 2),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Damco SEO Content Brief Generator")
    parser.add_argument("--coverage-gap", action="store_true",
                        help="Auto-pick top coverage-gap keywords (Damco missing from top 100, "
                             "at least 1 tracked competitor in top 10) by GSC impressions.")
    parser.add_argument("--keyword-ids",
                        help="Comma-separated keyword IDs (manual mode)")
    parser.add_argument("--offering", help="Restrict to one offering (default: all)")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT,
                        help=f"Max briefs to generate in --coverage-gap mode (default: {DEFAULT_LIMIT})")
    parser.add_argument("--no-llm", action="store_true",
                        help="Skip LLM enrichment; produce rule-based skeleton briefs only")
    parser.add_argument("--dry-run", action="store_true",
                        help="Write briefs to disk but skip DB inserts")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s  %(name)s  %(levelname)s  %(message)s",
    )

    kw_ids: list[int] | None = None
    if args.keyword_ids:
        try:
            kw_ids = [int(x.strip()) for x in args.keyword_ids.split(",") if x.strip()]
        except ValueError as exc:
            parser.error(f"--keyword-ids must be comma-separated integers: {exc}")

    if not args.coverage_gap and not kw_ids:
        parser.error("Provide either --coverage-gap or --keyword-ids")

    run(
        coverage_gap=args.coverage_gap,
        keyword_ids=kw_ids,
        offering=args.offering,
        limit=args.limit,
        allow_llm=not args.no_llm,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    main()
