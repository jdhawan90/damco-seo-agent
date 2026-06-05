"""
Guest Post Drafter — Phase 3 module of Off-Page & Links
========================================================

Drafts a guest post / third-party article for a target publication, on a
specific topic, with naturally embedded contextual links back to Damco.
Output is a writeable draft — the editor / sales lead sends it manually
after review. The agent never publishes.

The prompt is templated from the "SEO Article Prompt Template — Third-Party
Articles" deliverable (Damco brand style guide). All per-article variables
(blog title, audience, keywords, CTA URL, word count, reference article,
publishing platform) are parameterized; the FIXED rules from that template
(writing quality, statistics sourcing, em-dash max, no competitor links,
Oxford comma + Chicago style, ZeroGPT <25% target) are baked into the
generic system + user prompts and apply to every draft this module
produces.

Inputs
------
- A `platform_targets.id` (the publication we're pitching to)
- A topic (free-text) OR a content_briefs.id (reuse the brief's target)
- A primary keyword (search term to rank for) OR derived from the brief
- Optional: secondary keywords, blog title, target audience, reference URL,
  word count band, perspective (defaults supplied)

What the LLM produces
---------------------
Structured JSON, rendered to a final .docx-ready markdown:
  - Title (search-friendly, with primary kw)
  - Subtitle / dek
  - Author byline placeholder
  - Intro (no heading), then H2 / H3 body sections
  - 2-3 contextual links to the brand CTA URL embedded inline (naturally)
  - External statistics with inline source URLs (2025/2026 primary sources)
  - Conclusion (100-125 words) with natural CTA
  - Keyword frequency table
  - Sources list (with publication years for internal reference)
  - Image / SVG infographic cues

Compliance checks (applied to every draft)
------------------------------------------
- Word count: within the configured band (default 800-1200; raise to
  2000-2500 for long-form third-party articles)
- Brand CTA link count: 1-3 inline anchors to the configured CTA URL
- Inline anchor text MUST NOT be the bare target keyword
- Keyword density on primary keyword: 0.5%-2.5%
- Em dash count: <= 3 (Damco style rule)
- Banned phrases (style guide): "guaranteed", "fastest", "best", "#1",
  "game-changer", "cutting-edge", "seamless", "leverage" (verb),
  "in today's [landscape|world|environment]", "ultimately,", etc.
- >= 2 external citations with real URLs

Safety rules
------------
- Every draft is saved to `outputs/outreach/guest_posts/` AND logged to
  `offpage_activities` with status='draft'. Nothing publishes.
- Conservative-claims rule: see banned phrase list above.
- Platforms with status NOT IN ('active', 'pending') are rejected.

LLM cost
--------
~$0.06-0.25 per draft depending on length (Sonnet). Falls back to a
structured skeleton when Anthropic credit is unavailable.

Usage
-----
    # Topic-driven draft for a specific platform
    python -m offpage_links.guest_post_drafter \\
        --platform-id 7 \\
        --topic "Agentic AI architecture for insurance underwriting" \\
        --target-keyword "ai agent development" \\
        --damco-target-url https://www.damcogroup.com/ai-agent-development

    # Long-form third-party article (2000-2500 words)
    python -m offpage_links.guest_post_drafter --platform-id 7 --brief-id 42 \\
        --word-count-min 2000 --word-count-max 2500 \\
        --blog-title "How Data Validation Companies Are Becoming the Unsung Heroes of Agentic AI" \\
        --secondary-keywords "top data validation companies,best data validation companies" \\
        --reference-url https://example.com/related-article \\
        --target-audience "CIOs, CTOs, IT decision-makers"

    # Brief-driven (re-uses the brief's primary keyword + target URL)
    python -m offpage_links.guest_post_drafter --platform-id 7 --brief-id 42

    # Force rule-based (no LLM cost / when credit unavailable)
    python -m offpage_links.guest_post_drafter --platform-id 7 --topic "..." \\
        --target-keyword "..." --damco-target-url "..." --no-llm

    # Dry run: write file but skip DB writes
    python -m offpage_links.guest_post_drafter --platform-id 7 --brief-id 42 --dry-run
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from common.connectors.crawler import get_default_crawler
from common.database import connection, fetch_all, fetch_one, record_agent_run
from common.llm import LLMUnavailableError, call_claude


logger = logging.getLogger("guest_post_drafter")
AGENT_NAME = "offpage_links.guest_post_drafter"

OUTPUT_DIR = Path(__file__).resolve().parent.parent / "outputs" / "outreach" / "guest_posts"

BRAND_NAME              = "Damco Solutions"
BRAND_CTA_DEFAULT_URL   = "https://www.damcogroup.com/"
BRAND_CTA_DOMAIN        = "damcogroup.com"

DEFAULT_TARGET_AUDIENCE = "CIOs, CTOs, and IT decision-makers"
DEFAULT_PERSPECTIVE     = "second-person"   # "you, your" — keep consistent throughout
DEFAULT_WORD_COUNT_BAND = (800, 1200)
DEFAULT_CTA_LINK_MIN    = 1
DEFAULT_CTA_LINK_MAX    = 3
DEFAULT_MAX_EM_DASHES   = 3

DENSITY_BAND = (0.5, 2.5)             # percent — primary keyword density target band

# Banned promotional / over-claim phrases. Rejected by the style guide.
BANNED_CLAIM_PATTERNS = [
    r"\bguaranteed?\b", r"\bfastest\b", r"\bbest(?:-in-class)?\b",
    r"#\s*1\b", r"\bnumber\s+one\b",
    r"100%\s+(?:success|reliable|effective|guaranteed)",
    r"\bgame[-\s]?changer(?:s)?\b", r"\bcutting[-\s]?edge\b",
    r"\bdisrupt(?:s|ed|ing)?\b", r"\bsynerg(?:y|ies)\b",
    r"\bseamless(?:ly)?\b", r"\brobust\b",
    r"\bempower(?:s|ed|ing)?\b",
    r"\bleverage[sd]?\b",   # banned as a verb; "leverage" as a noun is allowed if it slips through
    r"\bworld[-\s]?class\b",
    r"\bin\s+today'?s\s+(?:fast[-\s]?paced|digital|complex|landscape|world|environment)\b",
]

# Banned default-AI sentence/paragraph openers (style guide).
BANNED_OPENERS = [
    r"^\s*it\s+is\s+(?:worth\s+noting|important\s+to\s+(?:note|understand))\s+that",
    r"^\s*this\s+(?:means|ensures|allows|is\s+where|is\s+why)\b",
    r"^\s*one\s+of\s+the\s+(?:key|most\s+important)",
    r"^\s*ultimately,?\s+",
    r"^\s*essentially,?\s+",
    r"^\s*fundamentally,?\s+",
    r"^\s*by\s+doing\s+so,?\s+",
    r"^\s*as\s+a\s+result,?\s+",
    r"^\s*let'?s\s+(?:explore|take\s+a\s+look\s+at|dive)",
    r"^\s*in\s+conclusion,?\s+",
    r"^\s*in\s+order\s+to\b",
    r"^\s*for\s+the\s+purpose\s+of\b",
    r"^\s*as\s+mentioned\s+(?:above|earlier|previously)\b",
]


# ---------------------------------------------------------------------------
# Read phase
# ---------------------------------------------------------------------------

def load_platform(platform_id: int) -> dict | None:
    return fetch_one(
        "SELECT id, platform_url, platform_name, domain_authority, niche, "
        "       contact_info, status "
        "  FROM platform_targets WHERE id = %s",
        [platform_id],
    )


def load_brief(brief_id: int) -> dict | None:
    return fetch_one(
        "SELECT id, target_url, keywords_json, brief_content "
        "  FROM content_briefs WHERE id = %s",
        [brief_id],
    )


def crawl_platform_context(platform_url: str) -> dict:
    target = platform_url if platform_url.startswith("http") else f"https://{platform_url}"
    try:
        result = get_default_crawler().fetch(target)
    except Exception as exc:
        logger.warning("platform crawl failed for %s: %s", target, exc)
        return {"title": None, "h1": None, "recent_topics": []}
    if not result.html:
        return {"title": None, "h1": None, "recent_topics": []}
    return {
        "title":         result.title,
        "h1":            (result.h1_tags[0] if result.h1_tags else None),
        "recent_topics": result.h2_tags[:10] if result.h2_tags else [],
        "url":           target,
    }


def resolve_from_brief(brief: dict) -> tuple[str, str, str]:
    """Returns (topic, target_keyword, damco_target_url)."""
    bc = brief.get("brief_content") or {}
    if isinstance(bc, str):
        try:
            bc = json.loads(bc)
        except json.JSONDecodeError:
            bc = {}
    target_section = bc.get("target") or {}
    primary_kw = target_section.get("primary_keyword") or ""
    target_url = brief.get("target_url") or ""
    # Topic: prefer the LLM-generated topic_angle if present; else the primary kw
    narrative = bc.get("narrative") or {}
    topic = narrative.get("topic_angle") or primary_kw or "(unspecified topic)"
    return topic, primary_kw, target_url


# ---------------------------------------------------------------------------
# LLM prompt
# ---------------------------------------------------------------------------

def make_system_prompt(brand_name: str) -> str:
    """Generic 'expert content writer' system prompt. Brand name is templated."""
    return (
        f"You are an expert content writer and strategist. You write SEO blogs, "
        f"guest posts, and paid-media articles that rank well and read well — "
        f"clear, direct, and human. Your writing sounds like an industry expert "
        f"at {brand_name}, not a content machine producing output. Every article "
        f"you produce should feel like it was written by a senior practitioner "
        f"who knows the subject deeply and respects the reader's time."
    )


def make_guest_post_prompt(platform: dict, context: dict, *,
                           topic: str,
                           blog_title: str,
                           primary_keyword: str,
                           secondary_keywords: list[str],
                           target_audience: str,
                           brand_name: str,
                           brand_cta_url: str,
                           word_count_band: tuple[int, int],
                           perspective: str,
                           reference_url: str | None,
                           max_em_dashes: int,
                           cta_link_min: int,
                           cta_link_max: int) -> str:
    """
    Generic third-party article prompt. Templated from the brand style guide.
    Every per-article variable is substituted in; every FIXED rule (writing
    quality, em-dash max, no competitor links, Oxford comma + Chicago style,
    ZeroGPT target, banned phrasing) is baked into the prompt body and applies
    to every draft this module produces.
    """
    recent = "\n".join(f"  - {t}" for t in (context.get("recent_topics") or [])[:8]) \
             or "  (no editorial context — infer from platform name and niche)"
    secondary_kw_line = ", ".join(secondary_keywords) if secondary_keywords else "(none specified)"
    reference_block = (
        f"\nREFERENCE ARTICLE (read first, then research the top 5-7 ranking articles\n"
        f"on the same theme and identify one meaningful content gap — a relevant angle,\n"
        f"data point, or perspective that none of the top-ranking articles cover\n"
        f"adequately. Add that as a distinct section. If no genuine gap exists,\n"
        f"do NOT force a section.):\n  URL: {reference_url}\n"
    ) if reference_url else ""

    target_word = (word_count_band[0] + word_count_band[1]) // 2

    return f"""Draft a third-party / guest article for the publication below.

================================================================================
1. ARTICLE BRIEF
================================================================================
Content type:        Third-party SEO article / guest post
Brand name:          {brand_name}
Publishing platform: {platform.get('platform_name') or platform['platform_url']}  ({platform['platform_url']})
Blog title:          {blog_title}
Target word count:   {word_count_band[0]}-{word_count_band[1]} words (aim ~{target_word}).
                     Do NOT exceed the upper limit. Quality over volume — if the
                     argument is made in fewer words, do not pad.
Target audience:     {target_audience}
Perspective:         {perspective} ("you, your") throughout. Do not mix first,
                     second, and third person within the same article.
Topic / angle:       {topic}

================================================================================
2. BRAND & STYLE REQUIREMENTS
================================================================================
Brand voice:         Optimistic, simple, inspirational. Upbeat and resourceful —
                     not boastful. Acknowledge the challenge, then focus on the
                     solution. No over-promising.
Grammar standard:    US English. Chicago Manual of Style.
Em dash rule:        Use em dashes sparingly. Maximum {max_em_dashes} per article.
                     If a comma or a restructured sentence works equally well,
                     prefer that instead.
Oxford comma:        Required in all lists of three or more items.
Numbers:             Spell out 1-9. Use numerals for 10 and above.
Proper nouns:        Capitalize product / platform names consistently throughout.
Paragraphs:          100-200 words each. Minimum 3 sentences. Maximum 5-6 sentences.
                     No standalone one-sentence paragraphs unless used deliberately
                     for emphasis.
Headings:            Title case for H2 / H3 / H4. Each heading must sound like
                     something a knowledgeable person would say — never a generic
                     content label. If a heading could appear in any article on
                     any topic, rewrite it. Avoid "Why X Matters", "Key Benefits
                     of X", "Understanding the Process".

================================================================================
3. PUBLISHING PLATFORM CONTEXT
================================================================================
Platform homepage title:   {context.get('title') or '(unknown)'}
Platform H1:               {context.get('h1') or '(unknown)'}
Recent editorial sections on the platform (match this register):
{recent}
{reference_block}
================================================================================
4. KEYWORDS
================================================================================
Primary keyword (use naturally; prioritize in the title, H2 headings, and
opening two paragraphs — do not stuff):
  - {primary_keyword}

Secondary keywords (use for semantic coverage throughout the body; they do not
need to appear in headings):
  - {secondary_kw_line}

At the end of the article, return a keyword frequency table listing each
keyword (primary + secondary) and the number of times it appears in the final
body text.

================================================================================
5. STATISTICS & LINKING RULES
================================================================================
- Use only recent statistics from 2025 or 2026 where available. Older data is
  allowed only if no recent equivalent exists from a credible source.
- Source all statistics from PRIMARY sources only: the brand's own published
  data, IBM, Gartner, McKinsey, Forrester, IDC, government bodies, or
  peer-reviewed research. NO listicle blogs, NO SEO content sites.
- Hyperlink every statistic inline to its source page. Provide the URL in the
  output JSON. Do not include publication years inline in the article body —
  list them in the sources block instead.
- Do NOT link to competitor websites under any circumstances.
- Brand CTA hyperlink: naturally embed the brand CTA URL ({brand_cta_url}) in
  {cta_link_min}-{cta_link_max} places throughout the article where it fits
  contextually. Do not force it.
- Inline brand-CTA anchor text MUST NOT be the bare primary keyword. Use a
  descriptive natural phrase (e.g. "our methodology for X", "a breakdown
  of Y").

================================================================================
6. WRITING QUALITY (THE MOST IMPORTANT SECTION — APPLY THROUGHOUT)
================================================================================
SENTENCE CONSTRUCTION
- Keep sentences short and focused. One idea per sentence.
- Subject-verb-object. Simple. Direct.
- If a sentence has two clauses joined by "and" / "but" / "which", consider
  splitting it in two.
- The reader should never have to re-read a sentence to understand it.

NATURAL FLOW & HUMAN VOICE
- The article should feel like it was written by one thoughtful person, not
  assembled from parts. Each section should lead naturally into the next.
- Vary your rhythm. Mix short punchy sentences (4-8 words) with mid-length
  (15-20). Use a longer sentence only when an idea genuinely requires it.
  A paragraph where every sentence is the same length reads as machine-generated.

TRANSITIONS & PHRASING VARIETY — AVOID THESE PATTERNS (they become invisible
from overuse):
- "This is where..." / "This is why..." / "This means..." (pick one, not all)
- "In today's [landscape / world / environment]..."
- "Let's explore..." / "Let's take a look at..."
- "It is worth noting that..." / "It is important to understand that..."
- "One of the key..." / "One of the most important..."
- "Ultimately,..." / "Essentially,..." / "Fundamentally,..." as paragraph openers
- "By doing so,..." / "As a result,..." used more than once per section
- "In conclusion," as the literal opening of the conclusion
- Starting more than two consecutive paragraphs with the same word or phrase
Instead: lead with a fact, a short observation, a direct command, a question,
or a consequence. Mix these openers across the article.

TONE RULES
- Affirmative over negative. State what something is, not what it is not.
- Rewrite "not X but Y" constructions as direct affirmative statements.
- Remove filler adverbs: "extremely", "very", "definitely", "truly", "simply"
  unless they carry specific meaning.
- BANNED clichés / buzzwords: "game-changer", "cutting-edge", "disrupt",
  "synergy", "leverage" (as a verb), "empower" (overused), "seamless",
  "robust", "scalable" used without specifics.
- Remove corporate padding: "In order to", "It is important to note that",
  "For the purpose of", "As mentioned above".
- BANNED over-claims: "guaranteed", "fastest", "best", "#1", "world-class",
  "100% success".

GROUNDED, CONCRETE WORD CHOICES
Replace abstract nouns with active, grounded language. AI defaults to abstract
nouns because they are flexible and statistically common.
- "organizations see improved efficiency" → "your service team handles the
  same case volume with fewer manual handoffs"
- "this leads to better outcomes" → "deals close faster because reps spend
  less time switching between tools"
- "the process is straightforward" → describe the actual first step
- "optimization of processes" → "cutting the steps your team repeats every day"

================================================================================
7. ARTICLE STRUCTURE
================================================================================
1. INTRO (no heading) — open with the business problem. Hook the reader in the
   first sentence. 2-3 short paragraphs. Do NOT introduce yourself or the topic.
   Start with the situation or the cost.

2. BODY SECTIONS (H2, H3 headings) — each section covers one clear idea. The
   last line of one section should make the next section feel necessary.
   For sections listing a process / steps / phases: use bullet points or
   numbered lists. Do not write these as dense paragraphs. When using bullet
   points or any pointer, use a COLON (not an em dash) before the description
   starts. If an SVG infographic would meaningfully aid comprehension of a
   process or data set, request one via `image_cues` (functional only — no
   decorative visuals).

3. CONCLUSION (mandatory) — 100-125 words MAX. Summarize the core argument in
   2-3 sentences. Include a natural CTA hyperlink to {brand_cta_url}. Close
   with a forward-looking statement.

================================================================================
8. HUMAN-STYLE WRITING (ZeroGPT TARGET: <= 25% AI-DETECTED)
================================================================================
The finished article must read as human-written. AI detectors flag text that
is statistically predictable: smooth transitions, symmetrical paragraph
lengths, perfectly resolved ideas, and generic phrasing. Address each pattern
directly:
- VARY SENTENCE LENGTH DELIBERATELY. Mix 4-8 word sentences with 15-20 word
  ones. Occasionally use a longer sentence.
- BE SPECIFIC, NOT GENERAL. Replace broad claims with concrete details.
  Generalities are the single biggest driver of high AI-detection scores.
- LET PARAGRAPHS BE UNEVEN. Some ideas need four sentences; some need two.
  Identical-length paragraphs are a strong AI signal.
- AVOID THE DEFAULT AI OPENERS (listed in section 6 above).
- DO NOT OVER-RESOLVE EVERY IDEA. Occasionally end a paragraph on an open
  note or a tension rather than a tidy summary sentence.
- AVOID SYMMETRICAL BULLET STRUCTURES. Some bullets should be short and
  punchy. Others should carry more context. Not every bullet must start with
  a verb. Vary the construction.

================================================================================
9. DELIVERABLE — OUTPUT FORMAT (JSON ONLY, NO PREAMBLE OR COMMENTARY)
================================================================================
Return a single JSON object with EXACTLY these fields:

{{
  "title":      "<final blog title — search-friendly, primary keyword used naturally>",
  "subtitle":   "<one-sentence dek/standfirst that captures the angle>",
  "byline":     "[Author name placeholder] — {brand_name}",
  "intro":      "<2-3 short paragraphs of intro WITHOUT a heading. Hook in the first sentence. Use \\n\\n between paragraphs.>",
  "sections": [
    {{
      "h2":    "<specific, non-generic H2 heading in Title Case>",
      "body":  "<body paragraphs separated by \\n\\n>",
      "h3_subsections": [
        {{ "h3": "<optional H3 heading>", "body": "<paragraphs under this H3>" }}
      ],
      "list": {{
        "kind":  "<bulleted | numbered | none>",
        "items": ["<each item is one paragraph; use colon (not em dash) before any description>"]
      }},
      "cta_link": {{
        "present":  <true | false>,
        "anchor":   "<3-8 word descriptive anchor if present — NOT the bare primary keyword>",
        "url":      "{brand_cta_url}"
      }}
    }}
  ],
  "conclusion": "<100-125 word conclusion. Includes the natural brand CTA hyperlink. Forward-looking close.>",
  "external_citations": [
    {{
      "claim":     "<the exact sentence in the body that this statistic supports>",
      "stat":      "<the statistic itself, no publication year inline>",
      "source":    "<primary source name, e.g. 'Gartner', 'IBM Institute for Business Value'>",
      "url":       "<real, live source URL>",
      "year":      "<publication year — kept here for the sources list, not inline>"
    }}
  ],
  "keyword_frequency": [
    {{ "keyword": "<keyword>", "frequency": <int> }}
  ],
  "image_cues": [
    "<one functional image / SVG infographic idea per entry; 2-4 entries>"
  ],
  "author_bio": "<50-80 word author bio for {brand_name}. May include one additional link to {BRAND_CTA_DEFAULT_URL} if natural.>"
}}

Across all sections, the keyword frequency table must reflect the actual final
body text. Count carefully. Do not include the JSON itself in those counts."""


def fallback_template(brand_name: str, brand_cta_url: str, topic: str,
                       primary_keyword: str, blog_title: str) -> dict:
    """Structural skeleton when LLM unavailable. Mirrors the JSON shape the
    prompt requests so downstream code paths stay uniform."""
    return {
        "title":    blog_title or f"[FILL: title featuring '{primary_keyword}']",
        "subtitle": "[FILL: one-sentence summary of the angle]",
        "byline":   f"[Author name placeholder] — {brand_name}",
        "intro":    (
            "[PLACEHOLDER — load Anthropic credit and re-run for a full LLM-drafted "
            "intro. Open with the business problem; do not introduce the topic.]"
        ),
        "sections": [
            {
                "h2":              f"[FILL: opening H2 specific to '{topic}']",
                "body":            "[PLACEHOLDER body paragraphs]",
                "h3_subsections":  [],
                "list":            {"kind": "none", "items": []},
                "cta_link":        {"present": False, "anchor": "", "url": brand_cta_url},
            },
            {"h2": "[FILL: H2 #2]", "body": "[Body paragraph]",
             "h3_subsections": [], "list": {"kind": "none", "items": []},
             "cta_link": {"present": False, "anchor": "", "url": brand_cta_url}},
            {"h2": "[FILL: H2 #3]", "body": "[Body paragraph]",
             "h3_subsections": [], "list": {"kind": "none", "items": []},
             "cta_link": {"present": False, "anchor": "", "url": brand_cta_url}},
            {"h2": f"[FILL: H2 #4 — references {brand_name} methodology]",
             "body": f"[Body paragraph with a contextual link to {brand_cta_url}]",
             "h3_subsections": [], "list": {"kind": "none", "items": []},
             "cta_link": {"present": True,
                          "anchor": "[FILL: 3-8 word descriptive anchor — not the bare keyword]",
                          "url": brand_cta_url}},
            {"h2": "[FILL: H2 #5 — concrete examples / takeaways]",
             "body": "[Body paragraph]",
             "h3_subsections": [], "list": {"kind": "none", "items": []},
             "cta_link": {"present": False, "anchor": "", "url": brand_cta_url}},
        ],
        "conclusion": (
            f"[FILL: 100-125 word conclusion summarizing the core argument. "
            f"Include a natural CTA hyperlink to {brand_cta_url}. Close with a "
            f"forward-looking statement.]"
        ),
        "external_citations": [
            {"claim": "[FILL: cited claim 1]", "stat": "[FILL]",
             "source": "[Gartner / IBM / McKinsey / Forrester / IDC]",
             "url": "[FILL: source URL 1]", "year": "[FILL]"},
            {"claim": "[FILL: cited claim 2]", "stat": "[FILL]",
             "source": "[Gartner / IBM / McKinsey / Forrester / IDC]",
             "url": "[FILL: source URL 2]", "year": "[FILL]"},
        ],
        "keyword_frequency": [
            {"keyword": primary_keyword, "frequency": 0},
        ],
        "image_cues": [
            "[Hero image cue]",
            "[Supporting diagram / SVG infographic cue]",
        ],
        "author_bio": (
            f"[FILL: 50-80 word author bio for {brand_name}. May include one "
            f"additional link to {BRAND_CTA_DEFAULT_URL} if natural.]"
        ),
        "_source": "rule-based (LLM not used or unavailable)",
    }


def generate_guest_post(platform: dict, context: dict, *,
                        topic: str,
                        blog_title: str,
                        primary_keyword: str,
                        secondary_keywords: list[str],
                        target_audience: str,
                        brand_name: str,
                        brand_cta_url: str,
                        word_count_band: tuple[int, int],
                        perspective: str,
                        reference_url: str | None,
                        max_em_dashes: int,
                        cta_link_min: int,
                        cta_link_max: int,
                        allow_llm: bool,
                        max_output_tokens: int = 8000) -> tuple[dict, dict | None]:
    """Build the prompt, call Claude, parse JSON. Falls back to skeleton on
    any failure. Returns (draft_dict, usage_dict_or_None)."""
    if not allow_llm:
        return fallback_template(brand_name, brand_cta_url, topic, primary_keyword, blog_title), None

    system_prompt = make_system_prompt(brand_name)
    user_prompt = make_guest_post_prompt(
        platform, context,
        topic=topic, blog_title=blog_title, primary_keyword=primary_keyword,
        secondary_keywords=secondary_keywords, target_audience=target_audience,
        brand_name=brand_name, brand_cta_url=brand_cta_url,
        word_count_band=word_count_band, perspective=perspective,
        reference_url=reference_url, max_em_dashes=max_em_dashes,
        cta_link_min=cta_link_min, cta_link_max=cta_link_max,
    )
    try:
        text, usage = call_claude(user_prompt, tier="default", system=system_prompt,
                                   max_tokens=max_output_tokens, temperature=0.7)
    except LLMUnavailableError as exc:
        logger.warning("LLM unavailable, using rule-based skeleton: %s", exc)
        return fallback_template(brand_name, brand_cta_url, topic, primary_keyword, blog_title), None

    block: dict = {}
    try:
        cleaned = re.sub(r"^```(?:json)?\s*", "", text.strip())
        cleaned = re.sub(r"\s*```\s*$", "", cleaned)
        first = cleaned.find("{")
        last = cleaned.rfind("}")
        if first >= 0 and last > first:
            block = json.loads(cleaned[first:last + 1])
    except (json.JSONDecodeError, ValueError) as exc:
        logger.warning("Failed to parse LLM JSON: %s", exc)

    if not block.get("title") or not block.get("sections"):
        fallback = fallback_template(brand_name, brand_cta_url, topic, primary_keyword, blog_title)
        fallback["_source"] = "rule-based (LLM JSON parse failed)"
        fallback["_llm_raw_first_500"] = text[:500]
        return fallback, usage

    # Normalize sections shape (tolerate missing optional fields)
    fixed_sections = []
    for s in block["sections"]:
        if not isinstance(s, dict) or not s.get("h2") or not s.get("body"):
            continue
        cta_link = s.get("cta_link") or {}
        if isinstance(cta_link, dict):
            cta_link = {
                "present": bool(cta_link.get("present")),
                "anchor":  str(cta_link.get("anchor") or "").strip(),
                "url":     str(cta_link.get("url") or brand_cta_url).strip(),
            }
        else:
            cta_link = {"present": False, "anchor": "", "url": brand_cta_url}
        list_block = s.get("list") or {"kind": "none", "items": []}
        if not isinstance(list_block, dict):
            list_block = {"kind": "none", "items": []}
        list_block = {
            "kind":  str(list_block.get("kind") or "none").lower(),
            "items": [str(i) for i in (list_block.get("items") or []) if i],
        }
        h3_subs = []
        for sub in (s.get("h3_subsections") or []):
            if isinstance(sub, dict) and sub.get("h3"):
                h3_subs.append({"h3": str(sub["h3"]).strip(),
                                "body": str(sub.get("body") or "").strip()})
        fixed_sections.append({
            "h2":              str(s["h2"]).strip(),
            "body":            str(s["body"]).strip(),
            "h3_subsections":  h3_subs,
            "list":            list_block,
            "cta_link":        cta_link,
        })
    if not fixed_sections:
        fixed_sections = fallback_template(brand_name, brand_cta_url, topic,
                                            primary_keyword, blog_title)["sections"]
    block["sections"] = fixed_sections

    block.setdefault("intro", "")
    block.setdefault("subtitle", "")
    block.setdefault("byline", f"[Author] — {brand_name}")
    block.setdefault("conclusion", "")
    block.setdefault("author_bio", "")
    block.setdefault("image_cues", [])
    block.setdefault("external_citations", [])
    block.setdefault("keyword_frequency", [])

    # Normalize external_citations & keyword_frequency shapes
    cleaned_cites = []
    for c in (block.get("external_citations") or []):
        if not isinstance(c, dict):
            continue
        cleaned_cites.append({
            "claim":  str(c.get("claim") or "").strip(),
            "stat":   str(c.get("stat") or "").strip(),
            "source": str(c.get("source") or "").strip(),
            "url":    str(c.get("url") or "").strip(),
            "year":   str(c.get("year") or "").strip(),
        })
    block["external_citations"] = cleaned_cites

    cleaned_kw = []
    for k in (block.get("keyword_frequency") or []):
        if not isinstance(k, dict):
            continue
        try:
            freq = int(k.get("frequency") or 0)
        except (TypeError, ValueError):
            freq = 0
        kw = str(k.get("keyword") or "").strip()
        if kw:
            cleaned_kw.append({"keyword": kw, "frequency": freq})
    block["keyword_frequency"] = cleaned_kw

    block["_source"] = f"LLM ({usage['model']})"
    return block, usage


# ---------------------------------------------------------------------------
# Compliance checks on the draft
# ---------------------------------------------------------------------------

def collect_full_text(draft: dict) -> str:
    """Concatenate intro + every section body (and h3 bodies + list items) +
    conclusion. Used for word count + keyword density + banned-phrase scan."""
    chunks: list[str] = []
    intro = (draft.get("intro") or "").strip()
    if intro:
        chunks.append(intro)
    for s in draft.get("sections", []):
        body = (s.get("body") or "").strip()
        if body:
            chunks.append(body)
        for sub in (s.get("h3_subsections") or []):
            sub_body = (sub.get("body") or "").strip()
            if sub_body:
                chunks.append(sub_body)
        items = ((s.get("list") or {}).get("items") or [])
        chunks.extend(str(i) for i in items if i)
    conclusion = (draft.get("conclusion") or "").strip()
    if conclusion:
        chunks.append(conclusion)
    return " \n\n ".join(chunks)


def count_keyword_in_text(text: str, keyword: str) -> int:
    if not keyword or not text:
        return 0
    pattern = r"\b" + r"\s+".join(re.escape(t) for t in keyword.lower().split()) + r"\b"
    return len(re.findall(pattern, text.lower()))


def count_brand_cta_links(draft: dict, brand_cta_url: str,
                           brand_cta_domain: str) -> int:
    """Count CTA-link occurrences across every section's cta_link, the
    conclusion (URL appears in body text), and the author bio."""
    if not brand_cta_url:
        return 0
    cta_host_match = re.search(r"(?:https?:)?//([^/]+)", brand_cta_url)
    cta_host = cta_host_match.group(1).lower() if cta_host_match else ""
    cta_root = cta_host[4:] if cta_host.startswith("www.") else cta_host
    if not cta_root:
        cta_root = brand_cta_domain.lower()

    count = 0
    for s in (draft.get("sections") or []):
        cta = (s.get("cta_link") or {})
        if cta.get("present") and cta.get("url") and cta_root in cta["url"].lower():
            count += 1

    # Conclusion + bio: look for URLs in their raw text
    for blob in (draft.get("conclusion") or "", draft.get("author_bio") or ""):
        urls = re.findall(r"https?://[^\s)]+", blob)
        count += sum(1 for u in urls if cta_root in u.lower())
    return count


def count_em_dashes(text: str) -> int:
    """Count em dashes (U+2014) only — NOT en dashes or hyphens."""
    return text.count("—")


def find_banned_openers(text: str) -> list[str]:
    """Return a list of human-readable opener-pattern hits across paragraphs."""
    hits: list[str] = []
    for para in re.split(r"\n\s*\n", text or ""):
        for pat in BANNED_OPENERS:
            if re.match(pat, para, re.IGNORECASE):
                hits.append(re.sub(r"\s+", " ", para[:80]).strip())
                break
    return hits


def run_compliance(draft: dict, primary_keyword: str, brand_cta_url: str, *,
                    word_count_band: tuple[int, int] = DEFAULT_WORD_COUNT_BAND,
                    cta_link_min: int = DEFAULT_CTA_LINK_MIN,
                    cta_link_max: int = DEFAULT_CTA_LINK_MAX,
                    max_em_dashes: int = DEFAULT_MAX_EM_DASHES) -> dict:
    issues: list[dict] = []
    full_text = collect_full_text(draft)
    wc = len(full_text.split())

    # 1. Word count band
    if not (word_count_band[0] <= wc <= word_count_band[1]):
        sev = "warn" if (wc >= word_count_band[0] * 0.7
                          and wc <= word_count_band[1] * 1.3) else "fail"
        issues.append({
            "severity": sev, "kind": "word_count",
            "detail":   f"Body is {wc} words; target {word_count_band[0]}-{word_count_band[1]}.",
        })

    # 2. Primary keyword density
    if primary_keyword:
        occurrences = count_keyword_in_text(full_text, primary_keyword)
        kw_words = len(primary_keyword.split())
        density = (100.0 * occurrences * kw_words / wc) if wc else 0
        if not (DENSITY_BAND[0] <= density <= DENSITY_BAND[1]):
            sev = "fail" if (density == 0 or density > DENSITY_BAND[1] * 1.5) else "warn"
            issues.append({
                "severity": sev, "kind": "keyword_density",
                "detail":   f"Density {density:.2f}% on '{primary_keyword}' "
                            f"(target {DENSITY_BAND[0]}-{DENSITY_BAND[1]}%, "
                            f"{occurrences} occurrence(s)).",
            })

    # 3. Brand CTA link count (band, not exact)
    cta_links = count_brand_cta_links(draft, brand_cta_url, BRAND_CTA_DOMAIN)
    if cta_links == 0:
        issues.append({"severity": "fail", "kind": "missing_cta_link",
                       "detail": f"No link to the brand CTA URL ({brand_cta_url}) found anywhere."})
    elif cta_links < cta_link_min:
        issues.append({"severity": "warn", "kind": "few_cta_links",
                       "detail": f"{cta_links} brand-CTA link(s); target {cta_link_min}-{cta_link_max}."})
    elif cta_links > cta_link_max:
        issues.append({"severity": "warn", "kind": "too_many_cta_links",
                       "detail": f"{cta_links} brand-CTA links (max {cta_link_max}). Reads as self-promotional."})

    # 4. CTA anchor MUST NOT be the bare primary keyword
    if primary_keyword:
        primary_lower = primary_keyword.lower().strip()
        for s in (draft.get("sections") or []):
            cta = s.get("cta_link") or {}
            anchor = (cta.get("anchor") or "").strip().lower()
            if cta.get("present") and anchor == primary_lower:
                issues.append({
                    "severity": "warn", "kind": "spammy_anchor",
                    "detail":   f"Inline CTA anchor is the bare primary keyword "
                                f"('{primary_keyword}'). Editors usually reject this; "
                                f"pick a descriptive phrase.",
                })
                break

    # 5. Em-dash count (style guide rule)
    em_dashes = count_em_dashes(full_text)
    if em_dashes > max_em_dashes:
        issues.append({
            "severity": "warn", "kind": "em_dashes",
            "detail":   f"{em_dashes} em dashes in body; brand rule caps at {max_em_dashes}.",
        })

    # 6. Banned claim / cliché / buzzword phrases
    for pat in BANNED_CLAIM_PATTERNS:
        if re.search(pat, full_text, re.IGNORECASE):
            issues.append({
                "severity": "warn", "kind": "claim_phrase",
                "detail":   f"Body contains banned phrase matching /{pat}/.",
            })

    # 7. Default AI openers
    opener_hits = find_banned_openers(full_text)
    for hit in opener_hits[:5]:
        issues.append({
            "severity": "warn", "kind": "ai_opener",
            "detail":   f"Paragraph opens with a default-AI pattern: '{hit}'",
        })

    # 8. Section count
    n_sections = len(draft.get("sections") or [])
    if n_sections < 5:
        issues.append({"severity": "warn", "kind": "structure",
                       "detail": f"Only {n_sections} H2 sections (target 5-8)."})

    # 9. External citations
    cites = [c for c in (draft.get("external_citations") or [])
             if c.get("url") and c.get("url").startswith("http")]
    if len(cites) < 2:
        issues.append({"severity": "warn", "kind": "citations",
                       "detail": f"Only {len(cites)} external citations with live URLs (target ≥2)."})

    # 10. Conclusion length (style guide: 100-125 words)
    conclusion_wc = len((draft.get("conclusion") or "").split())
    if conclusion_wc and not (100 <= conclusion_wc <= 125):
        issues.append({
            "severity": "warn", "kind": "conclusion_length",
            "detail":   f"Conclusion is {conclusion_wc} words (target 100-125).",
        })

    severity_order = {"fail": 2, "warn": 1, "info": 0}
    issues.sort(key=lambda i: -severity_order.get(i["severity"], 0))
    return {
        "word_count":     wc,
        "cta_links":      cta_links,
        "em_dashes":      em_dashes,
        "sections":       n_sections,
        "issues":         issues,
        "fail_count":     sum(1 for i in issues if i["severity"] == "fail"),
        "warn_count":     sum(1 for i in issues if i["severity"] == "warn"),
    }


# ---------------------------------------------------------------------------
# Write phase
# ---------------------------------------------------------------------------

def slugify(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-") or "draft"


def write_guest_post(platform: dict, topic: str, target_keyword: str,
                     draft: dict, compliance: dict) -> Path:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    name = f"{slugify(platform['platform_url'])}_{slugify(target_keyword or topic)[:50]}_{date.today().isoformat()}.md"
    path = OUTPUT_DIR / name

    p: list[str] = []
    p.append(f"# DRAFT — Third-Party Article for {platform['platform_url']}")
    p.append("")
    p.append(f"_Generated by `{AGENT_NAME}` on {date.today().isoformat()}._")
    p.append(f"_Source: {draft.get('_source', 'unknown')}_")
    p.append("")
    p.append(f"**Topic:** {topic}  ")
    p.append(f"**Target keyword:** `{target_keyword}`  ")
    p.append(f"**Word count:** {compliance['word_count']}  ")
    p.append(f"**Brand CTA links:** {compliance.get('cta_links', 0)}  ")
    p.append(f"**Em dashes:** {compliance.get('em_dashes', 0)}  ")
    p.append(f"**Sections:** {compliance['sections']}  ")
    p.append("")

    # Compliance flags inline (top of file)
    if compliance["issues"]:
        p.append("## ⚠️ Compliance flags  (review before sending)")
        p.append("")
        for i in compliance["issues"]:
            icon = "❌" if i["severity"] == "fail" else "⚠️"
            p.append(f"- {icon} **[{i['kind']}]** {i['detail']}")
        p.append("")

    p.append("---")
    p.append("")
    p.append(f"## {draft['title']}")
    if draft.get("subtitle"):
        p.append("")
        p.append(f"_{draft['subtitle']}_")
    p.append("")
    if draft.get("byline"):
        p.append(f"**By:** {draft['byline']}")
        p.append("")

    # Intro (no heading)
    if draft.get("intro"):
        p.append(draft["intro"])
        p.append("")

    # Sections
    for s in draft.get("sections") or []:
        p.append(f"### {s['h2']}")
        p.append("")
        body = s.get("body") or ""
        # If this section embeds a CTA link, wrap the anchor into a markdown link
        cta = s.get("cta_link") or {}
        if cta.get("present") and cta.get("anchor") and cta.get("url"):
            anchor = cta["anchor"]
            if anchor.lower() in body.lower():
                body = re.sub(
                    re.escape(anchor),
                    f"[{anchor}]({cta['url']})",
                    body, count=1, flags=re.IGNORECASE,
                )
            else:
                body = body.rstrip(".") + f". For more on this approach, see [{anchor}]({cta['url']})."
        if body:
            p.append(body)
            p.append("")

        # H3 subsections
        for sub in s.get("h3_subsections") or []:
            if not sub.get("h3"):
                continue
            p.append(f"#### {sub['h3']}")
            p.append("")
            if sub.get("body"):
                p.append(sub["body"])
                p.append("")

        # Optional list under this section
        list_block = s.get("list") or {}
        items = list_block.get("items") or []
        kind = list_block.get("kind") or "none"
        if items and kind in ("bulleted", "numbered"):
            for i, item in enumerate(items, 1):
                bullet = f"{i}." if kind == "numbered" else "-"
                p.append(f"{bullet} {item}")
            p.append("")

    # Conclusion
    if draft.get("conclusion"):
        p.append("### Conclusion")
        p.append("")
        p.append(draft["conclusion"])
        p.append("")

    # Author bio
    if draft.get("author_bio"):
        p.append("---")
        p.append("")
        p.append("**About the author**")
        p.append("")
        p.append(draft["author_bio"])
        p.append("")

    # Keyword frequency table
    kw_freq = draft.get("keyword_frequency") or []
    if kw_freq:
        p.append("---")
        p.append("")
        p.append("### Keyword frequency")
        p.append("")
        p.append("| Keyword | Frequency |")
        p.append("|---|---:|")
        for k in kw_freq:
            p.append(f"| `{k.get('keyword', '')}` | {k.get('frequency', 0)} |")
        p.append("")

    # Sources list (for internal reference; publication years kept here)
    cites = draft.get("external_citations") or []
    if cites:
        p.append("### Sources cited")
        p.append("")
        for i, c in enumerate(cites, 1):
            year = f" ({c['year']})" if c.get("year") else ""
            source = c.get("source") or ""
            stat = c.get("stat") or c.get("claim") or ""
            url = c.get("url") or ""
            p.append(f"{i}. **{source}**{year}: {stat} — <{url}>")
        p.append("")

    # Image cues
    if draft.get("image_cues"):
        p.append("### Image / infographic cues (for designer)")
        p.append("")
        for c in draft["image_cues"]:
            p.append(f"- {c}")
        p.append("")

    p.append("---")
    p.append("")
    p.append("_DRAFT — FOR INTERNAL REVIEW. Replace any `[FILL: ...]` markers, "
             "verify every citation URL is live (no 404s), check the ZeroGPT score "
             "is ≤25%, then submit via the platform's process._")

    path.write_text("\n".join(p), encoding="utf-8")
    return path


def log_guest_post_activity(platform: dict, file_path: Path,
                             topic: str, target_keyword: str,
                             damco_target_url: str) -> int:
    sql = """
        INSERT INTO offpage_activities
            (executive, activity_type, target_page_id, platform_id, platform,
             status, date, published_url, notes)
        VALUES (NULL, 'guest_post', %s, %s, %s, 'draft', CURRENT_DATE, NULL, %s)
        RETURNING id
    """
    # Try to attach to a pages row matching damco_target_url
    target_page_id = None
    if damco_target_url:
        row = fetch_one("SELECT id FROM pages WHERE url = %s", [damco_target_url])
        if row:
            target_page_id = row["id"]

    notes = (f"Auto-drafted by {AGENT_NAME}. "
             f"Topic: {topic!r}. Target kw: {target_keyword!r}. File: {file_path}")
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                sql,
                (target_page_id, platform["id"],
                 platform.get("platform_name") or platform["platform_url"], notes),
            )
            return cur.fetchone()[0]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(platform_id: int,
        topic: str | None = None,
        target_keyword: str | None = None,
        damco_target_url: str | None = None,
        brief_id: int | None = None,
        blog_title: str | None = None,
        secondary_keywords: list[str] | None = None,
        target_audience: str = DEFAULT_TARGET_AUDIENCE,
        brand_name: str = BRAND_NAME,
        word_count_band: tuple[int, int] = DEFAULT_WORD_COUNT_BAND,
        perspective: str = DEFAULT_PERSPECTIVE,
        reference_url: str | None = None,
        max_em_dashes: int = DEFAULT_MAX_EM_DASHES,
        cta_link_min: int = DEFAULT_CTA_LINK_MIN,
        cta_link_max: int = DEFAULT_CTA_LINK_MAX,
        no_crawl: bool = False,
        allow_llm: bool = True,
        dry_run: bool = False) -> dict:
    start = time.monotonic()

    platform = load_platform(platform_id)
    if not platform:
        logger.error("platform_id=%s not found", platform_id)
        return {"status": "error", "reason": "platform not found"}
    if platform["status"] not in ("active", "pending"):
        logger.error("Platform status is '%s' — refusing to draft.", platform["status"])
        return {"status": "error",
                "reason": f"platform status '{platform['status']}' disallows drafting"}

    # Resolve topic / keyword / URL — either explicitly or from a brief
    if brief_id is not None:
        brief = load_brief(brief_id)
        if not brief:
            logger.error("brief_id=%s not found", brief_id)
            return {"status": "error", "reason": "brief not found"}
        b_topic, b_kw, b_url = resolve_from_brief(brief)
        topic = topic or b_topic
        target_keyword = target_keyword or b_kw
        damco_target_url = damco_target_url or b_url
        # If brief has secondary keywords + the caller didn't override, use them
        if not secondary_keywords:
            bc = brief.get("brief_content") or {}
            if isinstance(bc, str):
                try:
                    bc = json.loads(bc)
                except json.JSONDecodeError:
                    bc = {}
            target_section = bc.get("target") or {}
            sec = target_section.get("secondary_keywords") or []
            secondary_keywords = [s.get("keyword") if isinstance(s, dict) else str(s)
                                  for s in sec if s]

    if not topic or not target_keyword or not damco_target_url:
        logger.error("Need --topic, --target-keyword, and --damco-target-url "
                     "(or --brief-id to derive them).")
        return {"status": "error", "reason": "incomplete inputs"}

    if not damco_target_url.startswith(("http://", "https://")):
        damco_target_url = BRAND_CTA_DEFAULT_URL.rstrip("/") + (
            damco_target_url if damco_target_url.startswith("/") else f"/{damco_target_url}"
        )

    secondary_keywords = secondary_keywords or []
    blog_title = blog_title or f"Guidance on {target_keyword.title()}"

    context = {} if no_crawl else crawl_platform_context(platform["platform_url"])

    draft, usage = generate_guest_post(
        platform, context,
        topic=topic, blog_title=blog_title, primary_keyword=target_keyword,
        secondary_keywords=secondary_keywords, target_audience=target_audience,
        brand_name=brand_name, brand_cta_url=damco_target_url,
        word_count_band=word_count_band, perspective=perspective,
        reference_url=reference_url, max_em_dashes=max_em_dashes,
        cta_link_min=cta_link_min, cta_link_max=cta_link_max,
        allow_llm=allow_llm,
    )
    compliance = run_compliance(
        draft, target_keyword, damco_target_url,
        word_count_band=word_count_band,
        cta_link_min=cta_link_min, cta_link_max=cta_link_max,
        max_em_dashes=max_em_dashes,
    )

    file_path = write_guest_post(platform, topic, target_keyword, draft, compliance)

    activity_id: int | None = None
    if not dry_run:
        activity_id = log_guest_post_activity(
            platform, file_path, topic, target_keyword, damco_target_url
        )

    duration = time.monotonic() - start
    if not dry_run:
        record_agent_run(
            agent_name=AGENT_NAME,
            status="success" if compliance["fail_count"] == 0 else "partial",
            records_processed=1,
            errors=[i["detail"] for i in compliance["issues"] if i["severity"] == "fail"][:5],
            duration_seconds=round(duration, 2),
            metadata={
                "platform_id":        platform_id,
                "brief_id":           brief_id,
                "activity_id":        activity_id,
                "platform_url":       platform["platform_url"],
                "topic":              topic,
                "blog_title":         blog_title,
                "target_keyword":     target_keyword,
                "secondary_keywords": secondary_keywords,
                "damco_target_url":   damco_target_url,
                "word_count_band":    list(word_count_band),
                "target_audience":    target_audience,
                "perspective":        perspective,
                "reference_url":      reference_url,
                "llm_used":           usage is not None,
                "llm_cost_usd":       round(usage["est_cost_usd"], 4) if usage else 0,
                "word_count":         compliance["word_count"],
                "cta_links":          compliance.get("cta_links", 0),
                "em_dashes":          compliance.get("em_dashes", 0),
                "fail_count":         compliance["fail_count"],
                "warn_count":         compliance["warn_count"],
                "file_path":          str(file_path),
            },
        )

    # Console summary
    print()
    print(f"  {'=' * 72}")
    print(f"   GUEST POST / 3RD-PARTY DRAFTER -- {date.today().isoformat()}")
    print(f"  {'=' * 72}")
    print()
    print(f"  Platform:        {platform['platform_url']}")
    print(f"  Blog title:      {blog_title[:90]}")
    print(f"  Topic:           {topic[:90]}")
    print(f"  Target kw:       {target_keyword}")
    print(f"  Secondary kw:    {', '.join(secondary_keywords)[:90] if secondary_keywords else '(none)'}")
    print(f"  Brand CTA URL:   {damco_target_url}")
    print(f"  Audience:        {target_audience}")
    print(f"  Word band:       {word_count_band[0]}-{word_count_band[1]}")
    print(f"  LLM:             {'on' if usage else 'off / unavailable'}")
    if usage:
        print(f"  LLM cost:        ~${usage['est_cost_usd']:.4f}")
    print(f"  Word count:      {compliance['word_count']}")
    print(f"  Brand CTA links: {compliance.get('cta_links', 0)}")
    print(f"  Em dashes:       {compliance.get('em_dashes', 0)}")
    print(f"  Fail / Warn:     {compliance['fail_count']} / {compliance['warn_count']}")
    if activity_id:
        print(f"  Activity row:    offpage_activities.id = {activity_id}")
    print(f"  Draft file:      {file_path}")
    print(f"  Duration:        {duration:.2f}s")
    if compliance["issues"]:
        print()
        print("  Compliance flags:")
        for i in compliance["issues"][:8]:
            icon = "FAIL" if i["severity"] == "fail" else "WARN"
            print(f"    [{icon}] [{i['kind']}] {i['detail'][:100]}")
    print()

    return {
        "status":           "success" if compliance["fail_count"] == 0 else "partial",
        "file_path":        str(file_path),
        "activity_id":      activity_id,
        "word_count":       compliance["word_count"],
        "cta_links":        compliance.get("cta_links", 0),
        "em_dashes":        compliance.get("em_dashes", 0),
        "fail_count":       compliance["fail_count"],
        "warn_count":       compliance["warn_count"],
        "llm_cost_usd":     round(usage["est_cost_usd"], 4) if usage else 0,
        "duration_seconds": round(duration, 2),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Damco Guest Post / Third-Party Article Drafter")
    parser.add_argument("--platform-id", type=int, required=True,
                        help="platform_targets.id of the publication")
    parser.add_argument("--topic",
                        help="Free-text topic / angle for the post (or use --brief-id)")
    parser.add_argument("--blog-title",
                        help="Final blog title. If omitted, a placeholder is derived from --target-keyword.")
    parser.add_argument("--target-keyword",
                        help="Primary search keyword the post should rank for")
    parser.add_argument("--secondary-keywords",
                        help="Comma-separated list of secondary keywords")
    parser.add_argument("--damco-target-url",
                        help="Brand CTA URL — Damco page to link to from the post")
    parser.add_argument("--brief-id", type=int,
                        help="content_briefs.id — derive topic / primary_kw / secondary_kw / target_url from this brief")
    parser.add_argument("--target-audience", default=DEFAULT_TARGET_AUDIENCE,
                        help=f"Target audience description (default: {DEFAULT_TARGET_AUDIENCE!r})")
    parser.add_argument("--brand-name", default=BRAND_NAME,
                        help=f"Brand name (default: {BRAND_NAME!r})")
    parser.add_argument("--word-count-min", type=int, default=DEFAULT_WORD_COUNT_BAND[0],
                        help=f"Minimum body word count (default: {DEFAULT_WORD_COUNT_BAND[0]})")
    parser.add_argument("--word-count-max", type=int, default=DEFAULT_WORD_COUNT_BAND[1],
                        help=f"Maximum body word count (default: {DEFAULT_WORD_COUNT_BAND[1]})")
    parser.add_argument("--perspective", default=DEFAULT_PERSPECTIVE,
                        help=f"Narrative perspective (default: {DEFAULT_PERSPECTIVE!r})")
    parser.add_argument("--reference-url",
                        help="Reference article URL to read before drafting (drives content-gap research)")
    parser.add_argument("--max-em-dashes", type=int, default=DEFAULT_MAX_EM_DASHES,
                        help=f"Max em dashes allowed in body (default: {DEFAULT_MAX_EM_DASHES})")
    parser.add_argument("--cta-link-min", type=int, default=DEFAULT_CTA_LINK_MIN,
                        help=f"Min brand CTA links in body (default: {DEFAULT_CTA_LINK_MIN})")
    parser.add_argument("--cta-link-max", type=int, default=DEFAULT_CTA_LINK_MAX,
                        help=f"Max brand CTA links in body (default: {DEFAULT_CTA_LINK_MAX})")
    parser.add_argument("--no-crawl", action="store_true",
                        help="Skip the platform homepage fetch")
    parser.add_argument("--no-llm", action="store_true",
                        help="Skip LLM enrichment; produce structural skeleton only")
    parser.add_argument("--dry-run", action="store_true",
                        help="Write file but skip DB writes")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s  %(name)s  %(levelname)s  %(message)s",
    )

    secondary_kws: list[str] = []
    if args.secondary_keywords:
        secondary_kws = [s.strip() for s in args.secondary_keywords.split(",") if s.strip()]

    if args.word_count_min > args.word_count_max:
        parser.error("--word-count-min must be <= --word-count-max")

    run(
        platform_id=args.platform_id,
        topic=args.topic,
        target_keyword=args.target_keyword,
        damco_target_url=args.damco_target_url,
        brief_id=args.brief_id,
        blog_title=args.blog_title,
        secondary_keywords=secondary_kws or None,
        target_audience=args.target_audience,
        brand_name=args.brand_name,
        word_count_band=(args.word_count_min, args.word_count_max),
        perspective=args.perspective,
        reference_url=args.reference_url,
        max_em_dashes=args.max_em_dashes,
        cta_link_min=args.cta_link_min,
        cta_link_max=args.cta_link_max,
        no_crawl=args.no_crawl,
        allow_llm=not args.no_llm,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    main()
