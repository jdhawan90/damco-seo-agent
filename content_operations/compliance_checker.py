"""
Compliance Checker — Phase 2 module of Content Operations
==========================================================

Takes a submitted draft page (live or staging URL) and scores it against
the SEO content brief that was generated for the same target. Produces
a structured scorecard, persists it to `compliance_checks`, and flags
specific issues for the writer / content lead to address before publish.

Designed to chain off `content_operations.brief_generator`:
brief_generator outputs the brief; writer drafts the page; this module
verifies the draft matches the brief before publish.

What gets checked
-----------------
For each submitted draft URL we score:
  1. Primary keyword presence (title, H1, first 100 words, meta description)
  2. Primary keyword density (target band: 0.5%-3.0%)
  3. Secondary keyword coverage (each must appear at least once)
  4. Title length (target: 50-60 chars; warn 30-70; fail outside)
  5. Meta description length (target: 140-160; warn 120-170; fail outside)
  6. H1 presence + uniqueness (exactly one H1 required)
  7. Heading outline coverage (each brief-required H2 topic appears)
  8. Internal links — count + presence of brief-suggested target pages
  9. Image alt text coverage (≥80% of body images have non-empty alt)
 10. Schema markup presence (any JSON-LD; FAQPage bonus for AEO)
 11. Word count vs. brief recommendation (warn if <70%, fail if <50%)
 12. AEO checklist signals (FAQ section, question-style headings, citations)

Each check produces an issue with a severity (`pass` / `warn` / `fail`),
a human-readable message, and an evidence snippet. The overall score
is a 0-100 weighted aggregate of the dimensions.

Outputs
-------
- compliance_checks row with overall_score, issues_json, structured
  per-dimension fields, and references to the brief + draft URL
- outputs/audits/compliance_<page-slug>_<date>.md — narrative report

Usage
-----
    # Score by brief id (auto-loads the brief + grabs its target_url)
    python -m content_operations.compliance_checker --brief-id 42

    # Score a specific URL against a brief (e.g. staging vs production)
    python -m content_operations.compliance_checker --brief-id 42 --url https://staging.damcogroup.com/data-enrichment-services

    # Score a URL without a brief — runs generic SEO checks only
    python -m content_operations.compliance_checker --url https://www.damcogroup.com/some-page

    # Dry run — write report but skip DB insert
    python -m content_operations.compliance_checker --brief-id 42 --dry-run

Design notes
------------
This module is intentionally rule-based. Everything we check has a
clear, deterministic answer; LLM judgment would introduce noise and
cost without improving accuracy. The brief_generator already used the
LLM to produce the *requirements*; this module's job is verification.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from common.connectors.crawler import CrawlResult, get_default_crawler
from common.database import connection, fetch_one, fetch_all, record_agent_run


logger = logging.getLogger("compliance_checker")
AGENT_NAME = "content_operations.compliance_checker"

OUTPUT_DIR = Path(__file__).resolve().parent.parent / "outputs" / "audits"

# Scoring weights — sum to 100. Tuned so missing the primary keyword in the
# title is worse than slightly-off keyword density.
DIMENSION_WEIGHTS: dict[str, int] = {
    "primary_keyword_placement":  18,   # title / H1 / first-100-words / meta
    "primary_keyword_density":     8,
    "secondary_keyword_coverage":  8,
    "title_length":                6,
    "meta_description":            8,
    "h1_structure":                6,
    "outline_coverage":            8,
    "internal_links":              8,
    "image_alt_text":              6,
    "schema_markup":               6,
    "word_count":                  8,
    "aeo_signals":                10,
}
assert sum(DIMENSION_WEIGHTS.values()) == 100, "DIMENSION_WEIGHTS must sum to 100"

# Severity → fraction of dimension weight earned. 'pass' = full, 'warn' = half,
# 'fail' = zero.
SEVERITY_CREDIT = {"pass": 1.0, "warn": 0.5, "fail": 0.0}

# Bands for length / density checks
TITLE_LEN_PASS = (50, 60)
TITLE_LEN_WARN = (30, 70)
META_LEN_PASS  = (140, 160)
META_LEN_WARN  = (120, 170)
KEYWORD_DENSITY_PASS = (0.5, 3.0)        # percent
KEYWORD_DENSITY_WARN = (0.3, 4.0)
ALT_COVERAGE_PASS = 0.80
ALT_COVERAGE_WARN = 0.60
WORD_COUNT_PASS = 0.85                    # ≥85% of target = pass
WORD_COUNT_WARN = 0.60                    # ≥60% = warn
MIN_INTERNAL_LINKS_PASS = 3               # absolute floor — 0 is always fail


@dataclass
class Issue:
    dimension: str
    severity: str                         # pass | warn | fail
    message: str
    evidence: str | None = None
    details: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "dimension": self.dimension,
            "severity":  self.severity,
            "message":   self.message,
            "evidence":  self.evidence,
            "details":   self.details,
        }


# ---------------------------------------------------------------------------
# Read phase
# ---------------------------------------------------------------------------

def load_brief(brief_id: int) -> dict | None:
    row = fetch_one(
        "SELECT id, page_id, target_url, keywords_json, brief_content, file_path, status "
        "FROM content_briefs WHERE id = %s",
        [brief_id],
    )
    if not row:
        return None
    # brief_content is JSONB → already a dict via psycopg2
    return row


def resolve_page_id(target_url: str) -> int | None:
    """If the URL is already in `pages`, return its id; otherwise create one."""
    if not target_url:
        return None
    row = fetch_one("SELECT id FROM pages WHERE url = %s", [target_url])
    if row:
        return row["id"]
    # Insert a minimal pages row so the compliance check can FK to something.
    # page_type stays NULL (per migration 005 — for human review).
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO pages (url, page_type) VALUES (%s, NULL) "
                "ON CONFLICT (url) DO UPDATE SET updated_at = now() "
                "RETURNING id",
                [target_url],
            )
            return cur.fetchone()[0]


# ---------------------------------------------------------------------------
# Crawl + text extraction
# ---------------------------------------------------------------------------

def fetch_draft(url: str) -> CrawlResult:
    """Crawler with parse_html=True returns a fully populated CrawlResult."""
    return get_default_crawler().fetch(url)


# A "first 100 words" extract requires plain text. Crawler stores word_count
# but not the visible text itself; we re-derive from html for the placement
# checks.
def extract_visible_text(html: str | None) -> str:
    if not html:
        return ""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript", "template"]):
        tag.decompose()
    return " ".join(soup.get_text(" ", strip=True).split())


def normalize_kw(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").lower()).strip()


def count_keyword_occurrences(text: str, keyword: str) -> int:
    """
    Match the full keyword as a sequence of word-boundary tokens. Tolerates
    multiple spaces. Case-insensitive.
    """
    if not keyword or not text:
        return 0
    pattern = r"\b" + r"\s+".join(re.escape(t) for t in keyword.lower().split()) + r"\b"
    return len(re.findall(pattern, text.lower()))


# ---------------------------------------------------------------------------
# Per-dimension checks
# ---------------------------------------------------------------------------

def check_primary_keyword_placement(primary: str, crawl: CrawlResult,
                                     visible_text: str) -> Issue:
    title  = (crawl.title or "").lower()
    h1     = " | ".join(crawl.h1_tags).lower() if crawl.h1_tags else ""
    meta   = (crawl.meta_description or "").lower()
    first  = " ".join(visible_text.lower().split()[:100])
    p_low  = primary.lower()

    found_in: list[str] = []
    if p_low in title: found_in.append("title")
    if p_low in h1:    found_in.append("h1")
    if p_low in meta:  found_in.append("meta_description")
    if p_low in first: found_in.append("first_100_words")

    # All four = pass. Three = warn. Fewer = fail.
    if len(found_in) >= 4:
        sev, msg = "pass", f"Primary keyword `{primary}` present in title, H1, meta, and first 100 words."
    elif len(found_in) == 3:
        missing = {"title", "h1", "meta_description", "first_100_words"} - set(found_in)
        sev, msg = "warn", f"Primary keyword in {len(found_in)}/4 priority spots — missing: {', '.join(sorted(missing))}."
    else:
        missing = {"title", "h1", "meta_description", "first_100_words"} - set(found_in)
        sev, msg = "fail", f"Primary keyword missing from {len(missing)} of 4 priority spots: {', '.join(sorted(missing))}."

    return Issue(
        dimension="primary_keyword_placement",
        severity=sev,
        message=msg,
        evidence=f"title='{(crawl.title or '')[:80]}' | h1='{(crawl.h1_tags or ['—'])[0][:80]}'",
        details={"found_in": found_in},
    )


def check_primary_keyword_density(primary: str, visible_text: str) -> Issue:
    word_count = len(visible_text.split()) or 1
    occurrences = count_keyword_occurrences(visible_text, primary)
    primary_word_count = len(primary.split())
    density_pct = round(100.0 * occurrences * primary_word_count / word_count, 2)

    if KEYWORD_DENSITY_PASS[0] <= density_pct <= KEYWORD_DENSITY_PASS[1]:
        sev = "pass"
        msg = f"Keyword density {density_pct:.2f}% is in the healthy range ({KEYWORD_DENSITY_PASS[0]}-{KEYWORD_DENSITY_PASS[1]}%)."
    elif KEYWORD_DENSITY_WARN[0] <= density_pct <= KEYWORD_DENSITY_WARN[1]:
        sev = "warn"
        msg = (f"Keyword density {density_pct:.2f}% is outside the ideal band "
               f"({KEYWORD_DENSITY_PASS[0]}-{KEYWORD_DENSITY_PASS[1]}%) but still acceptable.")
    elif density_pct == 0:
        sev = "fail"
        msg = f"Primary keyword `{primary}` does not appear in body text at all."
    elif density_pct < KEYWORD_DENSITY_WARN[0]:
        sev = "fail"
        msg = f"Keyword density {density_pct:.2f}% is too low (target: {KEYWORD_DENSITY_PASS[0]}-{KEYWORD_DENSITY_PASS[1]}%)."
    else:
        sev = "fail"
        msg = f"Keyword density {density_pct:.2f}% looks like keyword stuffing (target: {KEYWORD_DENSITY_PASS[0]}-{KEYWORD_DENSITY_PASS[1]}%)."

    return Issue(
        dimension="primary_keyword_density",
        severity=sev,
        message=msg,
        details={"density_pct": density_pct, "occurrences": occurrences, "word_count": word_count},
    )


def check_secondary_keyword_coverage(secondary: list[dict], visible_text: str) -> Issue:
    if not secondary:
        return Issue("secondary_keyword_coverage", "pass",
                     "No secondary keywords specified in brief — nothing to verify.",
                     details={"requested": 0, "covered": 0})

    covered = []
    missing = []
    for s in secondary:
        kw = s.get("keyword") if isinstance(s, dict) else str(s)
        if not kw:
            continue
        if count_keyword_occurrences(visible_text, kw) > 0:
            covered.append(kw)
        else:
            missing.append(kw)

    total = len(covered) + len(missing)
    if not total:
        return Issue("secondary_keyword_coverage", "pass",
                     "No usable secondary keywords found in brief.",
                     details={"requested": 0, "covered": 0})

    coverage_pct = round(100.0 * len(covered) / total, 1)
    if coverage_pct >= 80:
        sev = "pass"
        msg = f"{len(covered)}/{total} secondary keywords present ({coverage_pct}%)."
    elif coverage_pct >= 50:
        sev = "warn"
        msg = (f"Only {len(covered)}/{total} secondary keywords present ({coverage_pct}%). "
               f"Weave in the missing ones for topical depth.")
    else:
        sev = "fail"
        msg = (f"Only {len(covered)}/{total} secondary keywords present ({coverage_pct}%). "
               f"Page is missing critical topical signals.")

    return Issue(
        dimension="secondary_keyword_coverage",
        severity=sev,
        message=msg,
        details={"requested": total, "covered": len(covered),
                 "covered_keywords": covered[:20], "missing_keywords": missing[:20]},
    )


def check_title_length(crawl: CrawlResult) -> Issue:
    title = crawl.title or ""
    n = len(title)
    if not title:
        return Issue("title_length", "fail", "Page has no <title> tag.", evidence=None)

    if TITLE_LEN_PASS[0] <= n <= TITLE_LEN_PASS[1]:
        sev, msg = "pass", f"Title length {n} chars is in the ideal band ({TITLE_LEN_PASS[0]}-{TITLE_LEN_PASS[1]})."
    elif TITLE_LEN_WARN[0] <= n <= TITLE_LEN_WARN[1]:
        sev, msg = "warn", f"Title length {n} chars is outside ideal ({TITLE_LEN_PASS[0]}-{TITLE_LEN_PASS[1]}); fine but tighten if possible."
    else:
        sev = "fail"
        msg = (f"Title length {n} chars — Google truncates above ~60 and treats sub-30 as low-effort. "
               f"Target {TITLE_LEN_PASS[0]}-{TITLE_LEN_PASS[1]}.")

    return Issue("title_length", sev, msg, evidence=f"'{title[:120]}'", details={"length": n})


def check_meta_description(crawl: CrawlResult) -> Issue:
    meta = crawl.meta_description or ""
    n = len(meta)
    if not meta:
        return Issue("meta_description", "fail",
                     "Page has no meta description. Google will synthesize one — usually badly.",
                     evidence=None)

    if META_LEN_PASS[0] <= n <= META_LEN_PASS[1]:
        sev, msg = "pass", f"Meta description {n} chars is in the ideal band."
    elif META_LEN_WARN[0] <= n <= META_LEN_WARN[1]:
        sev, msg = "warn", f"Meta description {n} chars is outside ideal ({META_LEN_PASS[0]}-{META_LEN_PASS[1]}); fine, can tighten."
    else:
        sev = "fail"
        msg = f"Meta description {n} chars — out of range ({META_LEN_PASS[0]}-{META_LEN_PASS[1]} target)."

    return Issue("meta_description", sev, msg, evidence=f"'{meta[:200]}'", details={"length": n})


def check_h1(crawl: CrawlResult) -> Issue:
    n = len(crawl.h1_tags)
    if n == 0:
        return Issue("h1_structure", "fail", "Page has no H1 tag.", details={"count": 0})
    if n == 1:
        return Issue("h1_structure", "pass", "Exactly one H1 present (correct).",
                     evidence=f"'{crawl.h1_tags[0][:120]}'", details={"count": 1})
    return Issue("h1_structure", "warn",
                 f"Page has {n} H1 tags — should have exactly 1.",
                 evidence=" | ".join(crawl.h1_tags[:3])[:200],
                 details={"count": n})


def check_outline_coverage(must_include_topics: list[str],
                            h2_sections: list[str],
                            crawl: CrawlResult,
                            visible_text: str) -> Issue:
    """
    Verify brief's required H2 outline + must-include subtopics actually
    show up in the rendered page. We accept either:
      - the H2 itself (or a clear paraphrase) appearing in crawl.h2_tags, OR
      - the topic's distinctive tokens (≥3-char, non-generic) all appearing
        in the visible text within a reasonable window
    """
    targets = list(filter(None, (h2_sections or []) + (must_include_topics or [])))
    if not targets:
        return Issue("outline_coverage", "pass",
                     "Brief specified no outline topics — nothing to verify.",
                     details={"requested": 0, "covered": 0})

    h2_blob = " ".join(crawl.h2_tags).lower()
    body_blob = visible_text.lower()
    covered: list[str] = []
    missing: list[str] = []

    generic = {"the", "and", "for", "with", "of", "in", "to", "a", "an",
               "our", "your", "we", "is", "are", "how", "what", "why", "this", "that"}

    for t in targets:
        t_low = t.lower().strip()
        if not t_low:
            continue
        # Direct H2 match (substring tolerant)
        if t_low in h2_blob:
            covered.append(t)
            continue
        # Token coverage in body — at least 70% of distinctive tokens present
        tokens = [tok for tok in re.findall(r"[a-z0-9]+", t_low)
                  if len(tok) >= 3 and tok not in generic]
        if not tokens:
            covered.append(t)
            continue
        hits = sum(1 for tok in tokens if tok in body_blob)
        if hits / len(tokens) >= 0.7:
            covered.append(t)
        else:
            missing.append(t)

    total = len(covered) + len(missing)
    coverage_pct = round(100.0 * len(covered) / total, 1) if total else 100.0
    if coverage_pct >= 80:
        sev, msg = "pass", f"{len(covered)}/{total} brief outline topics present ({coverage_pct}%)."
    elif coverage_pct >= 50:
        sev, msg = "warn", f"Only {len(covered)}/{total} brief topics covered ({coverage_pct}%)."
    else:
        sev, msg = "fail", f"Only {len(covered)}/{total} brief topics covered ({coverage_pct}%). Page strays from the brief."

    return Issue(
        dimension="outline_coverage",
        severity=sev,
        message=msg,
        details={"requested": total, "covered": len(covered),
                 "covered_topics": covered[:20], "missing_topics": missing[:20]},
    )


def check_internal_links(suggested: list[dict], crawl: CrawlResult,
                          base_origin: str) -> Issue:
    internal_links = [l for l in crawl.links if l.get("is_internal")]
    n_internal = len(internal_links)

    # Strip query/fragment for stable comparison
    def norm(u: str) -> str:
        return re.sub(r"[?#].*$", "", (u or "")).rstrip("/").lower()

    found_urls = {norm(l["href"]) for l in internal_links}
    suggested_urls = []
    suggested_hits = []
    for s in suggested or []:
        target = s.get("url") if isinstance(s, dict) else None
        if not target:
            continue
        # Suggested URLs are typically path-relative; resolve against origin.
        full = target if target.startswith("http") else f"{base_origin.rstrip('/')}{target if target.startswith('/') else '/' + target}"
        suggested_urls.append(target)
        if norm(full) in found_urls or norm(target) in found_urls:
            suggested_hits.append(target)

    suggested_coverage_pct = (round(100.0 * len(suggested_hits) / len(suggested_urls), 1)
                              if suggested_urls else None)

    if n_internal < MIN_INTERNAL_LINKS_PASS:
        sev = "fail"
        msg = f"Only {n_internal} internal link(s) found — at least {MIN_INTERNAL_LINKS_PASS} expected on a service/pillar page."
    elif suggested_urls and (suggested_coverage_pct or 0) < 50:
        sev = "warn"
        msg = (f"{n_internal} internal links present, but only {len(suggested_hits)}/{len(suggested_urls)} "
               f"of the brief's suggested targets are linked.")
    else:
        sev = "pass"
        msg = f"{n_internal} internal links present"
        if suggested_urls:
            msg += f" — {len(suggested_hits)}/{len(suggested_urls)} brief-suggested targets covered."

    return Issue(
        dimension="internal_links",
        severity=sev,
        message=msg,
        details={"count": n_internal,
                 "suggested_total": len(suggested_urls),
                 "suggested_covered": len(suggested_hits),
                 "suggested_missing": [u for u in suggested_urls if u not in suggested_hits][:20]},
    )


def check_alt_text(crawl: CrawlResult) -> Issue:
    images = [i for i in crawl.images
              # Skip data: URIs and tracking pixels — they're not editorial images
              if i.get("src") and not (i["src"] or "").startswith("data:")]
    total = len(images)
    if not total:
        return Issue("image_alt_text", "pass",
                     "No body images on this page — nothing to alt-tag.",
                     details={"total": 0, "with_alt": 0})

    with_alt = sum(1 for i in images if i.get("alt"))
    coverage = with_alt / total

    if coverage >= ALT_COVERAGE_PASS:
        sev, msg = "pass", f"{with_alt}/{total} images have alt text ({coverage*100:.0f}%)."
    elif coverage >= ALT_COVERAGE_WARN:
        sev, msg = "warn", f"Only {with_alt}/{total} images have alt text ({coverage*100:.0f}%)."
    else:
        sev, msg = "fail", f"Only {with_alt}/{total} images have alt text ({coverage*100:.0f}%). Significant accessibility + SEO loss."

    return Issue("image_alt_text", sev, msg,
                 details={"total": total, "with_alt": with_alt,
                          "missing_examples": [i["src"] for i in images if not i.get("alt")][:5]})


def check_schema(crawl: CrawlResult) -> Issue:
    schemas = crawl.schema_jsonld or []
    if not schemas:
        return Issue("schema_markup", "fail",
                     "No JSON-LD schema markup detected. At minimum, add Organization or Service schema.",
                     details={"count": 0, "types": []})

    types: list[str] = []
    for s in schemas:
        t = s.get("@type")
        if isinstance(t, list):
            types.extend(str(x) for x in t)
        elif t:
            types.append(str(t))

    has_faq = any("faq" in t.lower() for t in types)
    has_article = any(t.lower() in ("article", "blogposting", "techarticle") for t in types)
    has_service = any(t.lower() in ("service", "organization", "professionalservice") for t in types)

    if has_faq:
        return Issue("schema_markup", "pass",
                     f"Schema present: {', '.join(sorted(set(types)))}. FAQPage detected — strong AEO signal.",
                     details={"count": len(schemas), "types": types, "has_faq": True})
    if has_article or has_service:
        return Issue("schema_markup", "warn",
                     f"Schema present ({', '.join(sorted(set(types)))}) but no FAQPage. Adding FAQPage improves AI Overview eligibility.",
                     details={"count": len(schemas), "types": types, "has_faq": False})
    return Issue("schema_markup", "warn",
                 f"Schema present ({', '.join(sorted(set(types)))}) but no Article/Service/FAQPage. Consider adding one.",
                 details={"count": len(schemas), "types": types, "has_faq": False})


def check_word_count(target: int | None, crawl: CrawlResult) -> Issue:
    actual = crawl.word_count or 0
    if not target:
        # No brief target — apply minimum floor (300 words)
        if actual < 300:
            return Issue("word_count", "fail",
                         f"Page is only {actual} words — too thin to rank.",
                         details={"actual": actual, "target": None})
        return Issue("word_count", "pass",
                     f"{actual} words (no brief target specified).",
                     details={"actual": actual, "target": None})

    ratio = actual / target
    if ratio >= WORD_COUNT_PASS:
        sev = "pass"
        msg = f"{actual} words vs. brief target {target} ({ratio*100:.0f}%)."
    elif ratio >= WORD_COUNT_WARN:
        sev = "warn"
        msg = f"{actual} words vs. brief target {target} ({ratio*100:.0f}%) — close but consider expansion."
    else:
        sev = "fail"
        msg = f"{actual} words vs. brief target {target} ({ratio*100:.0f}%) — page is too thin."

    return Issue("word_count", sev, msg,
                 details={"actual": actual, "target": target, "ratio_pct": round(ratio * 100, 1)})


def check_aeo_signals(crawl: CrawlResult, visible_text: str) -> Issue:
    """
    Verify the AEO checklist that's hardcoded into every brief. We check
    for the most-measurable items:
      - At least one heading phrased as a question (H1/H2)
      - Bulleted/numbered list present
      - FAQ section heuristic (H2 containing "FAQ" or "Frequently Asked")
      - At least 2 external citations (links to non-Damco domains)
    """
    headings = (crawl.h1_tags or []) + (crawl.h2_tags or [])
    question_headings = sum(1 for h in headings if "?" in h)

    # External citation heuristic: external links with descriptive anchors
    base_origin = re.search(r"^(?:https?:)?//[^/]+", crawl.final_url or crawl.url or "")
    base_host = base_origin.group(0).lower() if base_origin else ""
    external_links = [l for l in (crawl.links or []) if not l.get("is_internal")]

    has_faq_heading = any(re.search(r"\bfaq\b|frequently asked", h, re.IGNORECASE) for h in headings)

    # Bulleted/numbered list heuristic via HTML (already parsed away — quick re-check)
    has_list = False
    if crawl.html:
        has_list = ("<ul" in crawl.html.lower() or "<ol" in crawl.html.lower())

    signals = {
        "question_headings": question_headings,
        "has_lists":         has_list,
        "has_faq_heading":   has_faq_heading,
        "external_citations": len(external_links),
    }

    score = 0
    if question_headings >= 1: score += 1
    if has_list:               score += 1
    if has_faq_heading:        score += 1
    if len(external_links) >= 2: score += 1

    if score >= 4:
        sev = "pass"
        msg = "All four AEO signals present (question headings, lists, FAQ section, external citations)."
    elif score >= 2:
        sev = "warn"
        missing = []
        if not question_headings: missing.append("question-style headings")
        if not has_list:          missing.append("bulleted/numbered lists")
        if not has_faq_heading:   missing.append("FAQ section")
        if len(external_links) < 2: missing.append("external citations (need ≥2)")
        msg = f"{score}/4 AEO signals present. Missing: {', '.join(missing)}."
    else:
        sev = "fail"
        msg = f"Only {score}/4 AEO signals present. Page is unlikely to be cited by AI search."

    return Issue("aeo_signals", sev, msg, details=signals)


# ---------------------------------------------------------------------------
# Aggregate
# ---------------------------------------------------------------------------

def run_all_checks(brief: dict | None, crawl: CrawlResult, draft_url: str) -> tuple[list[Issue], dict]:
    """Run every dimension. Returns (issues, summary_dict)."""
    visible_text = extract_visible_text(crawl.html)
    base_origin_match = re.match(r"^(https?://[^/]+)", crawl.final_url or draft_url)
    base_origin = base_origin_match.group(1) if base_origin_match else ""

    # Pull brief fields if available
    if brief and brief.get("brief_content"):
        bc = brief["brief_content"]
        if isinstance(bc, str):
            try:
                bc = json.loads(bc)
            except json.JSONDecodeError:
                bc = {}
        target_section   = bc.get("target") or {}
        outline_section  = bc.get("outline") or {}
        primary_kw       = target_section.get("primary_keyword") or ""
        secondary_kw     = target_section.get("secondary_keywords") or []
        h2_sections      = outline_section.get("h2_sections") or []
        must_include     = outline_section.get("must_include_topics") or []
        internal_targets = bc.get("internal_links_suggested") or []
        target_words     = bc.get("recommended_word_count")
    else:
        primary_kw       = ""
        secondary_kw     = []
        h2_sections      = []
        must_include     = []
        internal_targets = []
        target_words     = None

    issues: list[Issue] = []

    if primary_kw:
        issues.append(check_primary_keyword_placement(primary_kw, crawl, visible_text))
        issues.append(check_primary_keyword_density(primary_kw, visible_text))
    else:
        issues.append(Issue("primary_keyword_placement", "warn",
                            "No brief provided — skipping primary keyword placement check.",
                            details={}))
        issues.append(Issue("primary_keyword_density", "warn",
                            "No brief provided — skipping keyword density check.",
                            details={}))

    issues.append(check_secondary_keyword_coverage(secondary_kw, visible_text))
    issues.append(check_title_length(crawl))
    issues.append(check_meta_description(crawl))
    issues.append(check_h1(crawl))
    issues.append(check_outline_coverage(must_include, h2_sections, crawl, visible_text))
    issues.append(check_internal_links(internal_targets, crawl, base_origin))
    issues.append(check_alt_text(crawl))
    issues.append(check_schema(crawl))
    issues.append(check_word_count(target_words, crawl))
    issues.append(check_aeo_signals(crawl, visible_text))

    score = compute_score(issues)
    sev_counts = Counter(i.severity for i in issues)

    # Header-level meta status: fail if title/meta-description fail, warn if either warns.
    title_sev = next((i.severity for i in issues if i.dimension == "title_length"), "pass")
    meta_sev  = next((i.severity for i in issues if i.dimension == "meta_description"), "pass")
    meta_status = "fail" if "fail" in (title_sev, meta_sev) else ("warn" if "warn" in (title_sev, meta_sev) else "pass")

    density = next((i.details.get("density_pct") for i in issues if i.dimension == "primary_keyword_density"), None)
    internal_count = next((i.details.get("count") for i in issues if i.dimension == "internal_links"), 0)

    return issues, {
        "overall_score":        round(score, 2),
        "severity_counts":      dict(sev_counts),
        "keyword_density":      density,
        "meta_status":          meta_status,
        "internal_links_count": internal_count,
        "primary_keyword":      primary_kw,
        "word_count":           crawl.word_count,
    }


def compute_score(issues: list[Issue]) -> float:
    earned = 0.0
    for i in issues:
        w = DIMENSION_WEIGHTS.get(i.dimension, 0)
        earned += w * SEVERITY_CREDIT.get(i.severity, 0.0)
    return earned


# ---------------------------------------------------------------------------
# Write phase
# ---------------------------------------------------------------------------

def insert_compliance_check(page_id: int, summary: dict, issues: list[Issue]) -> int:
    sql = """
        INSERT INTO compliance_checks
            (page_id, overall_score, issues_json,
             keyword_density, meta_status, internal_links_count)
        VALUES (%s, %s, %s::jsonb, %s, %s, %s)
        RETURNING id
    """
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                sql,
                (
                    page_id,
                    summary["overall_score"],
                    json.dumps([i.to_dict() for i in issues]),
                    summary.get("keyword_density"),
                    summary["meta_status"],
                    summary.get("internal_links_count") or 0,
                ),
            )
            return cur.fetchone()[0]


def slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s or "page"


def write_markdown_report(summary: dict, issues: list[Issue],
                           brief: dict | None, draft_url: str,
                           crawl: CrawlResult, check_id: int | None) -> Path:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    slug = slugify(summary.get("primary_keyword") or draft_url.rstrip("/").split("/")[-1])[:60]
    path = OUTPUT_DIR / f"compliance_{slug}_{date.today().isoformat()}.md"

    p: list[str] = []
    p.append(f"# Compliance Report — {summary.get('primary_keyword') or 'untitled draft'}")
    p.append("")
    p.append(f"_Draft URL: `{draft_url}`_")
    if brief:
        p.append(f"_Brief: `content_briefs.id={brief['id']}` (file: {brief.get('file_path') or '—'})_")
    p.append(f"_Generated by `{AGENT_NAME}` on {date.today().isoformat()}._")
    p.append("")

    p.append("## Overall score")
    p.append("")
    score = summary["overall_score"]
    verdict = "✅ Ready to publish" if score >= 85 else "⚠️ Revise before publish" if score >= 70 else "❌ Major work needed"
    p.append(f"### **{score} / 100** — {verdict}")
    p.append("")
    sev_counts = summary["severity_counts"]
    p.append(f"- Pass: {sev_counts.get('pass', 0)}   Warn: {sev_counts.get('warn', 0)}   Fail: {sev_counts.get('fail', 0)}")
    p.append(f"- Word count: {summary.get('word_count') or 0}")
    p.append(f"- Meta status: {summary['meta_status']}")
    if check_id:
        p.append(f"- DB row: `compliance_checks.id = {check_id}`")
    p.append("")

    # Issues — group by severity
    by_sev: dict[str, list[Issue]] = {"fail": [], "warn": [], "pass": []}
    for i in issues:
        by_sev.setdefault(i.severity, []).append(i)

    for sev_label, header in (("fail", "❌ Failures"), ("warn", "⚠️ Warnings"), ("pass", "✅ Passing")):
        bucket = by_sev.get(sev_label) or []
        if not bucket:
            continue
        p.append(f"## {header} ({len(bucket)})")
        p.append("")
        for i in bucket:
            p.append(f"### {i.dimension}")
            p.append("")
            p.append(f"- **Status:** {i.severity}")
            p.append(f"- **What:** {i.message}")
            if i.evidence:
                p.append(f"- **Evidence:** `{i.evidence}`")
            if i.details:
                # Show details inline for failures + warnings
                if sev_label in ("fail", "warn"):
                    p.append(f"- **Details:** `{json.dumps(i.details, default=str)[:400]}`")
            p.append("")

    path.write_text("\n".join(p), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(brief_id: int | None = None,
        url: str | None = None,
        dry_run: bool = False) -> dict:
    start = time.monotonic()

    brief: dict | None = None
    if brief_id:
        brief = load_brief(brief_id)
        if not brief:
            logger.error("brief_id=%s not found", brief_id)
            return {"status": "error", "reason": "brief not found"}

    draft_url = url or (brief.get("target_url") if brief else None)
    if not draft_url:
        logger.error("No --url provided and brief has no target_url.")
        return {"status": "error", "reason": "no url"}
    if not draft_url.startswith(("http://", "https://")):
        logger.error("URL must be absolute (with http(s)://): %s", draft_url)
        return {"status": "error", "reason": "url not absolute"}

    logger.info("Fetching draft: %s", draft_url)
    crawl = fetch_draft(draft_url)
    if crawl.error or not crawl.html:
        logger.error("Fetch failed: status=%s error=%s", crawl.status, crawl.error)
        return {"status": "error", "reason": f"fetch failed: {crawl.error or crawl.status}"}
    if not crawl.is_html:
        logger.error("URL did not return HTML: content_type=%s", crawl.content_type)
        return {"status": "error", "reason": "not html"}

    logger.info("Running %d compliance dimensions", len(DIMENSION_WEIGHTS))
    issues, summary = run_all_checks(brief, crawl, draft_url)

    page_id = resolve_page_id(crawl.final_url or draft_url)
    check_id: int | None = None
    if not dry_run and page_id:
        check_id = insert_compliance_check(page_id, summary, issues)
        logger.info("compliance_checks row inserted: id=%s", check_id)

    md_path = write_markdown_report(summary, issues, brief, draft_url, crawl, check_id)
    duration = time.monotonic() - start

    if not dry_run:
        record_agent_run(
            agent_name=AGENT_NAME,
            status="success",
            records_processed=1,
            errors=[],
            duration_seconds=round(duration, 2),
            metadata={
                "run_date":          date.today().isoformat(),
                "brief_id":          brief_id,
                "draft_url":         draft_url,
                "overall_score":     summary["overall_score"],
                "severity_counts":   summary["severity_counts"],
                "check_id":          check_id,
                "md_path":           str(md_path),
            },
        )

    # Console summary
    print()
    print(f"  {'=' * 72}")
    print(f"   COMPLIANCE CHECK -- {date.today().isoformat()}")
    print(f"  {'=' * 72}")
    print()
    print(f"  Draft URL:           {draft_url}")
    if brief:
        print(f"  Brief id:            {brief['id']}")
        kw = summary.get('primary_keyword')
        if kw:
            print(f"  Primary keyword:     {kw}")
    print(f"  Overall score:       {summary['overall_score']} / 100")
    sc = summary["severity_counts"]
    print(f"  Pass / Warn / Fail:  {sc.get('pass', 0)} / {sc.get('warn', 0)} / {sc.get('fail', 0)}")
    if check_id:
        print(f"  DB row:              compliance_checks.id={check_id}")
    print(f"  Report:              {md_path}")
    print(f"  Duration:            {duration:.2f}s")

    # Show top 5 failures inline (these are the writer's punch list)
    fails = [i for i in issues if i.severity == "fail"]
    if fails:
        print()
        print(f"  Failures to address:")
        for i in fails[:5]:
            print(f"    - [{i.dimension}] {i.message[:100]}")
    print()

    return {
        "status":           "success",
        "overall_score":    summary["overall_score"],
        "severity_counts":  summary["severity_counts"],
        "check_id":         check_id,
        "md_path":          str(md_path),
        "duration_seconds": round(duration, 2),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Damco SEO Content Compliance Checker")
    parser.add_argument("--brief-id", type=int,
                        help="content_briefs.id — load brief + use its target_url unless --url overrides")
    parser.add_argument("--url",
                        help="Explicit draft URL to check (overrides brief.target_url)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Write report but skip DB inserts")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s  %(name)s  %(levelname)s  %(message)s",
    )

    if not args.brief_id and not args.url:
        parser.error("Provide either --brief-id, --url, or both.")

    run(brief_id=args.brief_id, url=args.url, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
