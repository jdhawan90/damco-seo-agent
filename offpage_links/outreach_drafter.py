"""
Outreach Drafter — Phase 3 module of Off-Page & Links
======================================================

Generates a personalized outreach email + follow-up for a given
platform target. Output is a writeable draft — the executive sends it
manually. The agent never sends.

Inputs
------
- A `platform_targets.id` (discovered by `platform_finder`)
- A target page or offering (what we want a link to)

What the LLM produces
---------------------
- Subject line (≤80 chars)
- Personalized body (200–350 words)
  - Opening: references a specific piece of the platform's recent
    editorial (we crawl their site briefly to provide context)
  - Damco value prop tailored to their audience
  - Specific link request with context
  - Soft CTA
- One follow-up variant (60–120 words)

Safety rules
------------
- Every draft is saved to `outputs/outreach/` AND logged to
  `offpage_activities` with status='draft'. Nothing sends.
- The conservative-claims rule applies: no promises of specific
  ranking outcomes ("we'll get you to #1"), no fabricated stats.
- Platforms with status NOT IN ('active', 'pending') are rejected at
  the door.

LLM cost
--------
~$0.02-0.05 per draft (Sonnet via `common.llm`). Graceful fallback to
a templated skeleton when Anthropic credit is unavailable.

Usage
-----
    # Personalized pitch for a platform → a Damco service page
    python -m offpage_links.outreach_drafter --platform-id 7 --target-page-id 42

    # Pitch tied to an offering rather than a specific page
    python -m offpage_links.outreach_drafter --platform-id 7 --offering "AI"

    # Skip the platform-site crawl (faster; less personalized)
    python -m offpage_links.outreach_drafter --platform-id 7 --offering "AI" --no-crawl

    # Force rule-based (no LLM cost)
    python -m offpage_links.outreach_drafter --platform-id 7 --offering "AI" --no-llm

    # Dry run: write file but don't insert offpage_activities row
    python -m offpage_links.outreach_drafter --platform-id 7 --offering "AI" --dry-run
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


logger = logging.getLogger("outreach_drafter")
AGENT_NAME = "offpage_links.outreach_drafter"

OUTPUT_DIR = Path(__file__).resolve().parent.parent / "outputs" / "outreach"


# ---------------------------------------------------------------------------
# Read phase
# ---------------------------------------------------------------------------

def load_platform(platform_id: int) -> dict | None:
    return fetch_one(
        "SELECT id, platform_url, platform_name, domain_authority, niche, "
        "       contact_info, status, last_contacted "
        "  FROM platform_targets WHERE id = %s",
        [platform_id],
    )


def load_target_page(target_page_id: int) -> dict | None:
    return fetch_one(
        "SELECT id, url, page_type, title, offering FROM pages WHERE id = %s",
        [target_page_id],
    )


def load_offering_anchor_page(offering: str) -> dict | None:
    """Pick the strongest Damco page to pitch for an offering."""
    rows = fetch_all(
        """
        SELECT id, url, page_type, title
          FROM pages
         WHERE offering = %s AND title IS NOT NULL AND page_type IS NOT NULL
         ORDER BY CASE page_type
                    WHEN 'pillar'  THEN 1
                    WHEN 'service' THEN 2
                    WHEN 'home'    THEN 3
                    ELSE 4
                  END,
                  word_count DESC NULLS LAST
         LIMIT 1
        """,
        [offering],
    )
    return rows[0] if rows else None


def crawl_platform_context(platform_url: str) -> dict:
    """Best-effort fetch of the platform homepage to tune the pitch."""
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
        "recent_topics": result.h2_tags[:8] if result.h2_tags else [],
        "url":           target,
    }


# ---------------------------------------------------------------------------
# LLM prompt
# ---------------------------------------------------------------------------

OUTREACH_SYSTEM = (
    "You write outreach emails for B2B IT services and AI consulting firms. "
    "Conservative tone. No outlandish claims. Never promise specific ranking "
    "outcomes or numbers we don't have proof of. Cite the platform's own "
    "editorial when context is provided. Keep emails skimmable: short paragraphs."
)


def make_outreach_prompt(platform: dict, target_page: dict,
                          context: dict) -> str:
    recent = "\n".join(f"  - {t}" for t in (context.get("recent_topics") or [])[:6]) or "  (no editorial context available)"
    return f"""Draft a personalized outreach email for a Damco Group SEO link request.

PLATFORM
  URL:               {platform['platform_url']}
  Name:              {platform.get('platform_name') or platform['platform_url']}
  Niche:             {platform.get('niche') or '(unspecified)'}
  Their homepage title:  {context.get('title') or '(unknown)'}
  Their H1:              {context.get('h1') or '(unknown)'}
  Recent editorial topics they cover:
{recent}

TARGET DAMCO PAGE
  URL:        {target_page['url']}
  Title:      {target_page.get('title') or '(no title yet)'}
  Page type:  {target_page.get('page_type') or '(unspecified)'}
  Offering:   {target_page.get('offering') or '(unspecified)'}

Output a JSON object with these fields (and ONLY the JSON):

{{
  "subject":   "<≤80 char subject line, specific and personal, not generic>",
  "body":     "<200-350 word email body. Use \\n\\n between paragraphs. Opens by referencing the platform's editorial. Then introduces Damco's relevant capability (use the page above as the proof point). Then a specific link request — e.g. 'including our piece on X in your roundup on Y' or 'a guest post on Z'. Closes with a soft CTA. NO promises of specific outcomes.>",
  "followup": "<60-120 word follow-up email body, sent 7 days later if no reply. Lighter touch.>",
  "rationale": "<1-2 sentence internal note: why this platform is a good fit for this page, and what we're banking on.>"
}}"""


def fallback_template(platform: dict, target_page: dict, context: dict) -> dict:
    """Rule-based skeleton when LLM isn't available."""
    p_name = platform.get("platform_name") or platform["platform_url"]
    t_title = target_page.get("title") or target_page["url"]
    return {
        "subject":  f"Could fit {p_name}'s coverage of [TOPIC]",
        "body":     (
            f"Hi [editor name],\n\n"
            f"I've been following {p_name}'s coverage of [topic — see {context.get('h1') or 'your homepage'}], "
            f"and wanted to share a piece we recently published that might be a useful reference for your readers.\n\n"
            f"We just put together a deep-dive on {t_title} ({target_page['url']}). "
            f"It covers [outline points — fill in 2-3 specific value props for THIS platform's audience].\n\n"
            f"If it's a fit, we'd be glad to either contribute a guest piece on the topic or be included "
            f"in a roundup. Happy to tailor.\n\n"
            f"[Your name]\n[Damco Group | https://www.damcogroup.com]"
        ),
        "followup": (
            f"Hi [editor name] — just bumping this in case it got buried. "
            f"Happy to send a shorter excerpt or pitch a different angle if the original doesn't quite fit. "
            f"No pressure.\n\n[Your name]"
        ),
        "rationale": "[PLACEHOLDER — load Anthropic credit and re-run for LLM rationale]",
        "_source":   "rule-based (LLM not used or unavailable)",
    }


def generate_outreach(platform: dict, target_page: dict, context: dict,
                       allow_llm: bool) -> tuple[dict, dict | None]:
    if not allow_llm:
        return fallback_template(platform, target_page, context), None

    prompt = make_outreach_prompt(platform, target_page, context)
    try:
        text, usage = call_claude(prompt, tier="default", system=OUTREACH_SYSTEM,
                                   max_tokens=2000, temperature=0.7)
    except LLMUnavailableError as exc:
        logger.warning("LLM unavailable, using rule-based skeleton: %s", exc)
        return fallback_template(platform, target_page, context), None

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

    if not block.get("subject") or not block.get("body"):
        fallback = fallback_template(platform, target_page, context)
        fallback["_source"] = "rule-based (LLM JSON parse failed)"
        fallback["_llm_raw_first_500"] = text[:500]
        return fallback, usage

    block.setdefault("followup", "")
    block.setdefault("rationale", "")
    block["_source"] = f"LLM ({usage['model']})"
    return block, usage


# ---------------------------------------------------------------------------
# Write phase
# ---------------------------------------------------------------------------

def slugify(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-") or "draft"


def write_outreach_file(platform: dict, target_page: dict, draft: dict) -> Path:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    name = f"{slugify(platform['platform_url'])}_{slugify(target_page['url'].split('/')[-1] or 'home')}_{date.today().isoformat()}.md"
    path = OUTPUT_DIR / name

    p: list[str] = []
    p.append(f"# Outreach Draft — {platform['platform_url']}")
    p.append("")
    p.append(f"_Generated by `{AGENT_NAME}` on {date.today().isoformat()}._")
    p.append(f"_Source: {draft.get('_source', 'unknown')}_")
    p.append("")
    p.append(f"**Platform:** `{platform['platform_url']}`  ")
    p.append(f"**Target page:** `{target_page['url']}`  ")
    p.append(f"**Target page title:** {target_page.get('title') or '(none)'}  ")
    p.append("")
    p.append("---")
    p.append("")
    p.append("## Subject")
    p.append("")
    p.append(f"> {draft['subject']}")
    p.append("")
    p.append("## Body")
    p.append("")
    p.append(draft["body"])
    p.append("")
    p.append("---")
    p.append("")
    p.append("## Follow-up (send +7 days if no reply)")
    p.append("")
    p.append(draft.get("followup") or "_(no follow-up generated)_")
    p.append("")
    if draft.get("rationale"):
        p.append("---")
        p.append("")
        p.append("## Internal rationale (do not send)")
        p.append("")
        p.append(f"_{draft['rationale']}_")

    path.write_text("\n".join(p), encoding="utf-8")
    return path


def log_outreach_activity(platform: dict, target_page: dict, file_path: Path) -> int:
    sql = """
        INSERT INTO offpage_activities
            (executive, activity_type, target_page_id, platform_id, platform,
             status, date, published_url, notes)
        VALUES (NULL, 'outreach', %s, %s, %s, 'draft', CURRENT_DATE, NULL, %s)
        RETURNING id
    """
    notes = f"Auto-drafted by {AGENT_NAME}. File: {file_path}"
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                sql,
                (target_page["id"], platform["id"],
                 platform.get("platform_name") or platform["platform_url"], notes),
            )
            return cur.fetchone()[0]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(platform_id: int,
        target_page_id: int | None = None,
        offering: str | None = None,
        no_crawl: bool = False,
        allow_llm: bool = True,
        dry_run: bool = False) -> dict:
    start = time.monotonic()

    platform = load_platform(platform_id)
    if not platform:
        logger.error("platform_id=%s not found", platform_id)
        return {"status": "error", "reason": "platform not found"}
    if platform["status"] not in ("active", "pending"):
        logger.error("Platform status is '%s' — refusing to draft outreach.", platform["status"])
        return {"status": "error", "reason": f"platform status '{platform['status']}' disallows drafting"}

    if target_page_id is not None:
        target_page = load_target_page(target_page_id)
    elif offering:
        target_page = load_offering_anchor_page(offering)
    else:
        logger.error("Provide either --target-page-id or --offering.")
        return {"status": "error", "reason": "no target"}

    if not target_page:
        logger.error("No Damco target page resolved for the given inputs.")
        return {"status": "error", "reason": "no target page"}

    context = {} if no_crawl else crawl_platform_context(platform["platform_url"])

    draft, usage = generate_outreach(platform, target_page, context, allow_llm=allow_llm)
    file_path = write_outreach_file(platform, target_page, draft)

    activity_id: int | None = None
    if not dry_run:
        activity_id = log_outreach_activity(platform, target_page, file_path)

    duration = time.monotonic() - start
    if not dry_run:
        record_agent_run(
            agent_name=AGENT_NAME,
            status="success",
            records_processed=1,
            errors=[],
            duration_seconds=round(duration, 2),
            metadata={
                "platform_id":     platform_id,
                "target_page_id":  target_page["id"],
                "activity_id":     activity_id,
                "llm_used":        usage is not None,
                "llm_cost_usd":    round(usage["est_cost_usd"], 4) if usage else 0,
                "file_path":       str(file_path),
                "platform_url":    platform["platform_url"],
                "target_url":      target_page["url"],
            },
        )

    print()
    print(f"  {'=' * 72}")
    print(f"   OUTREACH DRAFTER -- {date.today().isoformat()}")
    print(f"  {'=' * 72}")
    print()
    print(f"  Platform:        {platform['platform_url']}")
    print(f"  Target page:     {target_page['url']}")
    print(f"  LLM:             {'on' if usage else 'off / unavailable'}")
    if usage:
        print(f"  LLM cost:        ~${usage['est_cost_usd']:.4f}")
    if activity_id:
        print(f"  Activity row:    offpage_activities.id = {activity_id}")
    print(f"  Draft file:      {file_path}")
    print(f"  Duration:        {duration:.2f}s")
    print()

    return {
        "status":           "success",
        "file_path":        str(file_path),
        "activity_id":      activity_id,
        "llm_cost_usd":     round(usage["est_cost_usd"], 4) if usage else 0,
        "duration_seconds": round(duration, 2),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Damco Outreach Draft Generator")
    parser.add_argument("--platform-id", type=int, required=True,
                        help="platform_targets.id")
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--target-page-id", type=int, help="pages.id to pitch")
    target.add_argument("--offering", help="Pick the strongest page for this offering")
    parser.add_argument("--no-crawl", action="store_true",
                        help="Skip the brief platform homepage fetch (faster, less personal)")
    parser.add_argument("--no-llm", action="store_true",
                        help="Skip LLM enrichment; produce templated skeleton only")
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
        target_page_id=args.target_page_id,
        offering=args.offering,
        no_crawl=args.no_crawl,
        allow_llm=not args.no_llm,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    main()
