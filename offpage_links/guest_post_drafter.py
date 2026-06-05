"""
Guest Post Drafter — Phase 3 module of Off-Page & Links
========================================================

Drafts an 800–1200 word guest post for a given target platform, on a
specific topic, with one contextual link back to a Damco page. Output
is a writeable draft — the editor / sales lead sends it manually after
review. The agent never publishes.

Inputs
------
- A `platform_targets.id` (the publication we're pitching to)
- A topic (free-text) OR a content_briefs.id (reuse the brief's target)
- A target keyword (the search term we want this post to rank for /
  pass authority for) OR derived from the brief

What the LLM produces
---------------------
Structured guest-post draft:
  - Title (≤70 chars, includes target keyword naturally)
  - Subtitle / dek
  - Author byline placeholder
  - 5-8 H2 sections with body copy (target: 1000 words)
  - 1 contextual link to Damco (with anchor + target URL)
  - Author bio (50–80 words) with one additional link
  - Suggested image cues (2-4)

Compliance checks (applied to every draft)
------------------------------------------
- Word count: 800-1200 inclusive (warn if outside, surface in report)
- Link count: exactly 1-2 to Damco (1 inline + 1 in bio); reject >2
- Anchor text: must NOT be the exact target keyword on the inline link
  (looks spammy to editors); allow it only in the bio link
- Keyword density on target keyword: 0.5%-2.5% (warn outside)
- No claim sentences containing "guaranteed", "fastest", "best", "#1"

Safety rules
------------
- Every draft is saved to `outputs/outreach/guest_posts/` AND logged to
  `offpage_activities` with status='draft'. Nothing publishes.
- Conservative-claims rule: see compliance check list above.
- Platforms with status NOT IN ('active', 'pending') are rejected.

LLM cost
--------
~$0.06-0.12 per draft (Sonnet, ~3-5k tokens out for a 1000-word post).
Falls back to a structured skeleton when Anthropic credit is unavailable.

Usage
-----
    # Topic-driven draft for a specific platform
    python -m offpage_links.guest_post_drafter \\
        --platform-id 7 \\
        --topic "Agentic AI architecture for insurance underwriting" \\
        --target-keyword "ai agent development" \\
        --damco-target-url https://www.damcogroup.com/ai-agent-development

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

TARGET_WORD_COUNT = 1000
WORD_COUNT_BAND  = (800, 1200)
DENSITY_BAND     = (0.5, 2.5)         # percent
MAX_DAMCO_LINKS  = 2
BANNED_CLAIM_PATTERNS = [
    r"\bguaranteed?\b", r"\bfastest\b", r"\bbest\b", r"#\s*1\b",
    r"100%\s+(?:success|reliable|effective)",
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

GUEST_POST_SYSTEM = (
    "You write B2B IT services thought-leadership guest posts for editorial "
    "publications. Conservative, evidence-based tone — no marketing fluff, "
    "no superlatives, no unfounded claims. Cite sources when you mention "
    "statistics. Headlines and section titles are specific, not generic. "
    "Match the publication's editorial register based on the context provided."
)


def make_guest_post_prompt(platform: dict, context: dict,
                           topic: str, target_keyword: str,
                           damco_url: str) -> str:
    recent = "\n".join(f"  - {t}" for t in (context.get("recent_topics") or [])[:8]) or "  (no editorial context available)"
    damco_anchor_suggestion = "our deep-dive on this topic"   # default — LLM may refine
    return f"""Draft a guest post for the publication below.

PUBLICATION
  URL:        {platform['platform_url']}
  Name:       {platform.get('platform_name') or platform['platform_url']}
  Niche:      {platform.get('niche') or '(unspecified)'}
  Homepage title:   {context.get('title') or '(unknown)'}
  Homepage H1:      {context.get('h1') or '(unknown)'}
  Recent editorial sections (use as tone reference):
{recent}

ASSIGNMENT
  Topic:                {topic}
  Target keyword:       {target_keyword!r} (use in title + first 200 words + at most 1-2 H2s — naturally, no stuffing)
  Damco target URL:     {damco_url}
  Author org:           Damco Group (B2B IT services + AI consulting)

CONSTRAINTS
  - Body length: {WORD_COUNT_BAND[0]}-{WORD_COUNT_BAND[1]} words (target ~{TARGET_WORD_COUNT}). Count carefully.
  - Exactly 1 inline link back to Damco target URL, mid-body, in a contextual
    paragraph. Anchor MUST NOT be the bare target keyword — pick a natural
    descriptive phrase ("our methodology for X", "a breakdown of Y", etc.).
  - 5-8 H2 sections. Avoid generic openers like "What is X" / "Why X matters" —
    be specific to the topic.
  - No phrases: "guaranteed", "fastest", "best", "#1", "100% success".
  - Cite at least 2 external sources (with URLs, not faked).

OUTPUT a JSON object with EXACTLY these fields (and only the JSON):

{{
  "title":      "<=70 char title that mentions the target keyword naturally>",
  "subtitle":   "<one-sentence dek/standfirst summarizing the angle>",
  "byline":     "[Author name placeholder] — Damco Group",
  "sections":   [
    {{ "h2": "<specific H2>", "body": "<150-200 word paragraph(s); \\n\\n between paras>" }},
    ... 5 to 8 entries, totaling 800-1200 words across all bodies ...
  ],
  "inline_link": {{
    "anchor":     "<3-8 word natural anchor — NOT the bare target keyword>",
    "target_url": "{damco_url}",
    "in_section_index": <0-based index of the section the link appears in>
  }},
  "external_citations": [
    {{ "claim": "<the sentence/stat being cited>", "url": "<real source URL>" }},
    ... 2 to 4 entries ...
  ],
  "author_bio": "<50-80 word author bio block. May include one additional link to Damco's homepage.>",
  "image_cues": [
    "<image idea 1>", "<image idea 2>", ... 2-4 entries ...
  ]
}}"""


def fallback_template(platform: dict, topic: str, target_keyword: str,
                       damco_url: str) -> dict:
    return {
        "title":    f"[FILL: title featuring '{target_keyword}']",
        "subtitle": "[FILL: one-sentence summary of the angle]",
        "byline":   "[Author name placeholder] — Damco Group",
        "sections": [
            {
                "h2":   f"[FILL: opening H2 specific to '{topic}']",
                "body": (
                    f"[PLACEHOLDER — load Anthropic credit and re-run for a full "
                    f"LLM-drafted guest post. This skeleton mirrors the brief shape: "
                    f"a writer can use it as a structural template.]"
                ),
            },
            {"h2": "[FILL: H2 #2]", "body": "[Body paragraph]"},
            {"h2": "[FILL: H2 #3]", "body": "[Body paragraph]"},
            {"h2": "[FILL: H2 #4]", "body": "[Body paragraph]"},
            {"h2": "[FILL: H2 #5 — references Damco methodology]",
             "body": f"[Body paragraph with a contextual link to {damco_url}]"},
            {"h2": "[FILL: H2 #6 — concrete examples / takeaways]",
             "body": "[Body paragraph]"},
        ],
        "inline_link": {
            "anchor":           "[FILL: 3-8 word descriptive anchor — not the bare keyword]",
            "target_url":       damco_url,
            "in_section_index": 4,
        },
        "external_citations": [
            {"claim": "[FILL: cited claim 1]", "url": "[FILL: source URL 1]"},
            {"claim": "[FILL: cited claim 2]", "url": "[FILL: source URL 2]"},
        ],
        "author_bio": (
            "[FILL: 50-80 word author bio for Damco Group. May include one "
            "additional link to https://www.damcogroup.com.]"
        ),
        "image_cues": [
            "[Hero image cue]",
            "[Supporting diagram cue]",
        ],
        "_source": "rule-based (LLM not used or unavailable)",
    }


def generate_guest_post(platform: dict, context: dict, topic: str,
                        target_keyword: str, damco_url: str,
                        allow_llm: bool) -> tuple[dict, dict | None]:
    if not allow_llm:
        return fallback_template(platform, topic, target_keyword, damco_url), None

    prompt = make_guest_post_prompt(platform, context, topic, target_keyword, damco_url)
    try:
        text, usage = call_claude(prompt, tier="default", system=GUEST_POST_SYSTEM,
                                   max_tokens=6000, temperature=0.7)
    except LLMUnavailableError as exc:
        logger.warning("LLM unavailable, using rule-based skeleton: %s", exc)
        return fallback_template(platform, topic, target_keyword, damco_url), None

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
        fallback = fallback_template(platform, topic, target_keyword, damco_url)
        fallback["_source"] = "rule-based (LLM JSON parse failed)"
        fallback["_llm_raw_first_500"] = text[:500]
        return fallback, usage

    # Normalize sections shape
    fixed_sections = []
    for s in block["sections"]:
        if isinstance(s, dict) and s.get("h2") and s.get("body"):
            fixed_sections.append({"h2": str(s["h2"]).strip(), "body": str(s["body"]).strip()})
    block["sections"] = fixed_sections or fallback_template(platform, topic, target_keyword, damco_url)["sections"]

    block.setdefault("subtitle", "")
    block.setdefault("byline", "[Author] — Damco Group")
    block.setdefault("author_bio", "")
    block.setdefault("image_cues", [])
    block.setdefault("external_citations", [])
    block.setdefault("inline_link", {
        "anchor": "our methodology", "target_url": damco_url, "in_section_index": 0,
    })
    block["_source"] = f"LLM ({usage['model']})"
    return block, usage


# ---------------------------------------------------------------------------
# Compliance checks on the draft
# ---------------------------------------------------------------------------

def compute_word_count(draft: dict) -> int:
    text = " ".join(s.get("body", "") for s in draft.get("sections", []))
    return len(text.split())


def count_keyword_in_text(text: str, keyword: str) -> int:
    if not keyword or not text:
        return 0
    pattern = r"\b" + r"\s+".join(re.escape(t) for t in keyword.lower().split()) + r"\b"
    return len(re.findall(pattern, text.lower()))


def count_damco_links(draft: dict, damco_url: str) -> int:
    """Count inline_link + any bio links pointing to Damco hosts."""
    damco_host_match = re.search(r"(?:https?:)?//([^/]+)", damco_url or "")
    if not damco_host_match:
        return 0
    damco_host = damco_host_match.group(1).lower()
    damco_root = damco_host[4:] if damco_host.startswith("www.") else damco_host

    count = 0
    inline = (draft.get("inline_link") or {}).get("target_url") or ""
    if damco_root in inline.lower():
        count += 1

    # Count any URL in author_bio matching damco roots
    bio = draft.get("author_bio") or ""
    urls = re.findall(r"https?://[^\s)]+", bio)
    for u in urls:
        if damco_root in u.lower():
            count += 1
    return count


def run_compliance(draft: dict, target_keyword: str, damco_url: str) -> dict:
    issues: list[dict] = []
    full_text = " ".join(s.get("body", "") for s in draft.get("sections", []))
    wc = len(full_text.split())

    # Word count
    if not (WORD_COUNT_BAND[0] <= wc <= WORD_COUNT_BAND[1]):
        issues.append({
            "severity": "warn", "kind": "word_count",
            "detail":   f"Body is {wc} words; target {WORD_COUNT_BAND[0]}-{WORD_COUNT_BAND[1]}.",
        })

    # Keyword density
    if target_keyword:
        occurrences = count_keyword_in_text(full_text, target_keyword)
        kw_words = len(target_keyword.split())
        density = (100.0 * occurrences * kw_words / wc) if wc else 0
        if not (DENSITY_BAND[0] <= density <= DENSITY_BAND[1]):
            sev = "fail" if (density == 0 or density > DENSITY_BAND[1] * 1.5) else "warn"
            issues.append({
                "severity": sev, "kind": "keyword_density",
                "detail":   f"Density {density:.2f}% on '{target_keyword}' "
                            f"(target {DENSITY_BAND[0]}-{DENSITY_BAND[1]}%).",
            })

    # Damco link count
    damco_links = count_damco_links(draft, damco_url)
    if damco_links == 0:
        issues.append({"severity": "fail", "kind": "missing_link",
                       "detail": "No link back to Damco found in body or bio."})
    elif damco_links > MAX_DAMCO_LINKS:
        issues.append({"severity": "fail", "kind": "too_many_links",
                       "detail": f"{damco_links} Damco links found (max {MAX_DAMCO_LINKS})."})

    # Inline anchor isn't the bare target keyword
    inline = draft.get("inline_link") or {}
    inline_anchor = (inline.get("anchor") or "").strip().lower()
    if target_keyword and inline_anchor == target_keyword.lower():
        issues.append({
            "severity": "warn", "kind": "spammy_anchor",
            "detail":   f"Inline anchor is the bare target keyword '{target_keyword}'. "
                        f"Editors usually reject this; pick a descriptive phrase.",
        })

    # Banned-claim scan
    for pat in BANNED_CLAIM_PATTERNS:
        if re.search(pat, full_text, re.IGNORECASE):
            issues.append({
                "severity": "warn", "kind": "claim_phrase",
                "detail":   f"Body contains banned promotional phrase matching /{pat}/.",
            })

    # Section count
    n_sections = len(draft.get("sections") or [])
    if n_sections < 5:
        issues.append({"severity": "warn", "kind": "structure",
                       "detail": f"Only {n_sections} H2 sections (target 5-8)."})

    # External citations
    cites = [c for c in (draft.get("external_citations") or [])
             if c.get("url") and c.get("url").startswith("http")]
    if len(cites) < 2:
        issues.append({"severity": "warn", "kind": "citations",
                       "detail": f"Only {len(cites)} external citations (target ≥2)."})

    severity_order = {"fail": 2, "warn": 1, "info": 0}
    issues.sort(key=lambda i: -severity_order.get(i["severity"], 0))
    return {
        "word_count":     wc,
        "damco_links":    damco_links,
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
    p.append(f"# DRAFT — Guest Post for {platform['platform_url']}")
    p.append("")
    p.append(f"_Generated by `{AGENT_NAME}` on {date.today().isoformat()}._")
    p.append(f"_Source: {draft.get('_source', 'unknown')}_")
    p.append("")
    p.append(f"**Topic:** {topic}  ")
    p.append(f"**Target keyword:** `{target_keyword}`  ")
    p.append(f"**Word count:** {compliance['word_count']}  ")
    p.append(f"**Damco links:** {compliance['damco_links']}  ")
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

    inline_link = draft.get("inline_link") or {}
    inline_section_idx = inline_link.get("in_section_index", -1)

    for idx, s in enumerate(draft.get("sections") or []):
        p.append(f"### {s['h2']}")
        p.append("")
        body = s["body"]
        # If this is the inline-link section, wrap the anchor into a markdown link
        if idx == inline_section_idx and inline_link.get("anchor") and inline_link.get("target_url"):
            anchor = inline_link["anchor"]
            if anchor.lower() in body.lower():
                # Replace first case-insensitive occurrence
                body = re.sub(
                    re.escape(anchor),
                    f"[{anchor}]({inline_link['target_url']})",
                    body, count=1, flags=re.IGNORECASE,
                )
            else:
                # Append a sentence at end with the link
                body = body.rstrip(".") + f". For more on this approach, see [{anchor}]({inline_link['target_url']})."
        p.append(body)
        p.append("")

    # External citations
    cites = draft.get("external_citations") or []
    if cites:
        p.append("### Sources cited")
        p.append("")
        for c in cites:
            p.append(f"- {c.get('claim', '')} — <{c.get('url', '')}>")
        p.append("")

    # Author bio
    if draft.get("author_bio"):
        p.append("---")
        p.append("")
        p.append("**About the author**")
        p.append("")
        p.append(draft["author_bio"])
        p.append("")

    # Image cues
    if draft.get("image_cues"):
        p.append("---")
        p.append("")
        p.append("### Image cues (for designer)")
        p.append("")
        for c in draft["image_cues"]:
            p.append(f"- {c}")
        p.append("")

    p.append("---")
    p.append("")
    p.append("_DRAFT — FOR INTERNAL REVIEW. Replace `[FILL: ...]` markers, "
             "verify citation URLs, run a final read for the publication's voice, "
             "then send via the platform's submission process._")

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

    if not topic or not target_keyword or not damco_target_url:
        logger.error("Need --topic, --target-keyword, and --damco-target-url "
                     "(or --brief-id to derive them).")
        return {"status": "error", "reason": "incomplete inputs"}

    if not damco_target_url.startswith(("http://", "https://")):
        damco_target_url = "https://www.damcogroup.com" + (
            damco_target_url if damco_target_url.startswith("/") else f"/{damco_target_url}"
        )

    context = {} if no_crawl else crawl_platform_context(platform["platform_url"])

    draft, usage = generate_guest_post(
        platform, context, topic, target_keyword, damco_target_url, allow_llm=allow_llm
    )
    compliance = run_compliance(draft, target_keyword, damco_target_url)

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
                "platform_id":      platform_id,
                "brief_id":         brief_id,
                "activity_id":      activity_id,
                "platform_url":     platform["platform_url"],
                "topic":            topic,
                "target_keyword":   target_keyword,
                "damco_target_url": damco_target_url,
                "llm_used":         usage is not None,
                "llm_cost_usd":     round(usage["est_cost_usd"], 4) if usage else 0,
                "word_count":       compliance["word_count"],
                "fail_count":       compliance["fail_count"],
                "warn_count":       compliance["warn_count"],
                "file_path":        str(file_path),
            },
        )

    # Console summary
    print()
    print(f"  {'=' * 72}")
    print(f"   GUEST POST DRAFTER -- {date.today().isoformat()}")
    print(f"  {'=' * 72}")
    print()
    print(f"  Platform:        {platform['platform_url']}")
    print(f"  Topic:           {topic[:90]}")
    print(f"  Target kw:       {target_keyword}")
    print(f"  Damco URL:       {damco_target_url}")
    print(f"  LLM:             {'on' if usage else 'off / unavailable'}")
    if usage:
        print(f"  LLM cost:        ~${usage['est_cost_usd']:.4f}")
    print(f"  Word count:      {compliance['word_count']}")
    print(f"  Fail / Warn:     {compliance['fail_count']} / {compliance['warn_count']}")
    if activity_id:
        print(f"  Activity row:    offpage_activities.id = {activity_id}")
    print(f"  Draft file:      {file_path}")
    print(f"  Duration:        {duration:.2f}s")
    if compliance["issues"]:
        print()
        print("  Compliance flags:")
        for i in compliance["issues"][:5]:
            icon = "FAIL" if i["severity"] == "fail" else "WARN"
            print(f"    [{icon}] [{i['kind']}] {i['detail'][:100]}")
    print()

    return {
        "status":           "success" if compliance["fail_count"] == 0 else "partial",
        "file_path":        str(file_path),
        "activity_id":      activity_id,
        "word_count":       compliance["word_count"],
        "fail_count":       compliance["fail_count"],
        "warn_count":       compliance["warn_count"],
        "llm_cost_usd":     round(usage["est_cost_usd"], 4) if usage else 0,
        "duration_seconds": round(duration, 2),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Damco Guest Post Drafter")
    parser.add_argument("--platform-id", type=int, required=True,
                        help="platform_targets.id of the publication")
    parser.add_argument("--topic", help="Free-text topic for the post (or use --brief-id)")
    parser.add_argument("--target-keyword",
                        help="Search keyword the post should rank/pass authority for")
    parser.add_argument("--damco-target-url",
                        help="Damco URL to link to from the post body")
    parser.add_argument("--brief-id", type=int,
                        help="content_briefs.id — derive topic / target_kw / target_url from this brief")
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

    run(
        platform_id=args.platform_id,
        topic=args.topic,
        target_keyword=args.target_keyword,
        damco_target_url=args.damco_target_url,
        brief_id=args.brief_id,
        no_crawl=args.no_crawl,
        allow_llm=not args.no_llm,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    main()
