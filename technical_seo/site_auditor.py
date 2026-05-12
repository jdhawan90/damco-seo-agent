"""
Site Auditor — Phase 4 of Technical SEO agent
==============================================

Standard agent lifecycle:
  Read    — fetch eligible pages from the `pages` table (page_type filter,
            cadence-aware via pages.last_audited)
  Process — crawler.fetch() each page in parallel; run all detectors;
            collect issue records per page
  Write   — open new issues in `technical_issues` (with per-issue dedup);
            auto-resolve issues no longer triggered; update
            `pages.last_audited`; log `agent_runs`
  Notify  — console summary with severity breakdown and top-offending URLs

Detectors implemented
---------------------
  Title:        missing_title, short_title, long_title
  Meta:         missing_meta_description, short_meta_description,
                long_meta_description
  Headings:     missing_h1, multiple_h1
  Canonical:    missing_canonical, canonical_mismatch, canonical_external
  Images:       missing_alt_text (page-level: details.missing_count, examples)
  Content:      thin_content (page-type-aware word-count thresholds)
  Schema:       missing_schema (no JSON-LD AND no microdata)
  Indexability: noindex_meta (only flagged on home/pillar/service)
  Redirects:    redirect_chain_too_long (>2 hops)

Folded-in scope from the original "canonical_checker.py" module:
  canonical_mismatch, canonical_external, redirect_chain_too_long.

Usage
-----
    # Default: 3 domains, page_type IN (home, pillar, service), weekly cadence
    python -m technical_seo.site_auditor

    # One domain
    python -m technical_seo.site_auditor --domain damcogroup.com

    # Include blog + resource pages too
    python -m technical_seo.site_auditor --page-types home,pillar,service,blog,resource

    # Force re-audit ignoring cadence
    python -m technical_seo.site_auditor --all

    # Dry run — fetch + analyze but don't write to DB
    python -m technical_seo.site_auditor --dry-run
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from common.connectors.crawler import Crawler, CrawlResult
from common.database import connection, fetch_all, record_agent_run


logger = logging.getLogger("site_auditor")

AGENT_NAME = "technical_seo.site_auditor"

DEFAULT_PAGE_TYPES = ("home", "pillar", "service")
DEFAULT_CADENCE_DAYS = 7
DEFAULT_WORKERS = 4

# Title length sweet spot — Google truncates around 60 chars on desktop.
TITLE_MIN_CHARS = 30
TITLE_MAX_CHARS = 60
META_DESC_MIN_CHARS = 70
META_DESC_MAX_CHARS = 160

# Word-count thresholds for thin_content, by page_type.
THIN_CONTENT_THRESHOLDS = {
    "home":     150,   # homepages can be lighter on copy
    "pillar":   800,
    "service":  300,
    "blog":     300,
    "resource": 200,
    "landing":  100,
    "glossary": 100,
}

# Redirect chains > this many hops are flagged.
REDIRECT_MAX_HOPS = 2

# Severity by issue_type. Used to populate technical_issues.severity.
SEVERITY = {
    "missing_title":              "critical",
    "short_title":                "low",
    "long_title":                 "low",
    "missing_meta_description":   "high",
    "short_meta_description":     "low",
    "long_meta_description":      "low",
    "missing_h1":                 "high",
    "multiple_h1":                "medium",
    "missing_canonical":          "medium",
    "canonical_mismatch":         "high",
    "canonical_external":         "medium",
    "missing_alt_text":           "medium",
    "thin_content":               "medium",
    "missing_schema":             "low",
    "noindex_meta":               "high",
    "redirect_chain_too_long":    "medium",
    "invalid_schema":             "medium",
    # Cross-page detectors (run as a post-pass after per-page audit completes)
    "duplicate_title":              "high",
    "duplicate_meta_description":   "medium",
}


# ---------------------------------------------------------------------------
# Detectors
# ---------------------------------------------------------------------------

def detect_title_issues(r: CrawlResult) -> list[dict]:
    if r.title is None:
        return [_issue("missing_title", {})]
    n = len(r.title)
    issues = []
    if n < TITLE_MIN_CHARS:
        issues.append(_issue("short_title", {"length": n, "min": TITLE_MIN_CHARS, "title": r.title}))
    elif n > TITLE_MAX_CHARS:
        issues.append(_issue("long_title", {"length": n, "max": TITLE_MAX_CHARS, "title": r.title}))
    return issues


def detect_meta_description_issues(r: CrawlResult) -> list[dict]:
    if r.meta_description is None:
        return [_issue("missing_meta_description", {})]
    n = len(r.meta_description)
    issues = []
    if n < META_DESC_MIN_CHARS:
        issues.append(_issue("short_meta_description",
                             {"length": n, "min": META_DESC_MIN_CHARS, "meta_description": r.meta_description}))
    elif n > META_DESC_MAX_CHARS:
        issues.append(_issue("long_meta_description",
                             {"length": n, "max": META_DESC_MAX_CHARS, "meta_description": r.meta_description}))
    return issues


def detect_heading_issues(r: CrawlResult) -> list[dict]:
    issues = []
    if not r.h1_tags:
        issues.append(_issue("missing_h1", {}))
    elif len(r.h1_tags) > 1:
        issues.append(_issue("multiple_h1", {"count": len(r.h1_tags), "h1_samples": r.h1_tags[:3]}))
    return issues


def detect_canonical_issues(r: CrawlResult) -> list[dict]:
    issues = []
    rendered = r.final_url or r.url
    if r.canonical is None:
        issues.append(_issue("missing_canonical", {"rendered_url": rendered}))
        return issues

    # Compare canonical to the rendered URL (after redirects). Allow trailing-slash
    # variance — most CMSes are tolerant of it.
    canon = r.canonical.rstrip("/")
    rendered_clean = rendered.rstrip("/") if rendered else ""
    canon_origin = _origin(r.canonical)
    rendered_origin = _origin(rendered)

    if canon_origin != rendered_origin:
        issues.append(_issue("canonical_external",
                             {"canonical": r.canonical, "rendered_url": rendered,
                              "rendered_origin": rendered_origin, "canonical_origin": canon_origin}))
        return issues  # external canonical is a stronger signal — don't also fire mismatch

    if canon != rendered_clean:
        issues.append(_issue("canonical_mismatch",
                             {"canonical": r.canonical, "rendered_url": rendered}))
    return issues


def detect_alt_text_issues(r: CrawlResult) -> list[dict]:
    """
    Flag images missing alt text. Excludes `data:` URIs (typically lazy-load
    placeholder SVGs that get swapped client-side — not real images requiring
    alt) so the count reflects real accessibility/SEO gaps.
    """
    if not r.images:
        return []
    # Real images only — skip placeholder data: URIs.
    real_images = [i for i in r.images if not (i.get("src") or "").startswith("data:")]
    if not real_images:
        return []
    missing = [i for i in real_images if not i.get("alt")]
    if not missing:
        return []
    return [_issue("missing_alt_text", {
        "missing_count":  len(missing),
        "total_images":   len(real_images),
        "examples":       [i["src"] for i in missing[:5]],
    })]


def detect_thin_content(r: CrawlResult, page_type: str | None) -> list[dict]:
    if page_type is None:
        return []
    threshold = THIN_CONTENT_THRESHOLDS.get(page_type)
    if threshold is None or r.word_count >= threshold:
        return []
    return [_issue("thin_content", {
        "word_count":   r.word_count,
        "threshold":    threshold,
        "page_type":    page_type,
    })]


def detect_schema_issues(r: CrawlResult) -> list[dict]:
    """
    Two checks:
      1. missing_schema — no JSON-LD AND no microdata
      2. invalid_schema — known @types are missing required fields per
         schema.org. Only validates types we explicitly model; other types
         pass through silently.
    """
    if not r.schema_jsonld and not r.has_microdata:
        return [_issue("missing_schema", {})]

    # Validate known types in JSON-LD blocks.
    issues: list[dict] = []
    validation_problems: list[dict] = []
    for block in r.schema_jsonld:
        # Blocks can use @graph for multiple entities, or be a single entity.
        entities = block.get("@graph") if isinstance(block, dict) and "@graph" in block else [block]
        for entity in entities or []:
            if not isinstance(entity, dict):
                continue
            problems = _validate_schema_entity(entity)
            if problems:
                validation_problems.extend(problems)

    if validation_problems:
        issues.append(_issue("invalid_schema", {
            "problems":      validation_problems[:20],
            "problem_count": len(validation_problems),
        }))
    return issues


# Required-field map per schema.org @type. Conservative — only common types
# we'd expect to encounter on Damco's properties. Field names follow
# schema.org's casing.
SCHEMA_REQUIRED_FIELDS: dict[str, tuple[str, ...]] = {
    "Organization":      ("name", "url"),
    "WebPage":           ("name",),
    "WebSite":           ("name", "url"),
    "BreadcrumbList":    ("itemListElement",),
    "Service":           ("name",),
    "Product":           ("name",),
    "Article":           ("headline", "author"),
    "BlogPosting":       ("headline", "author"),
    "FAQPage":           ("mainEntity",),
    "Question":          ("name", "acceptedAnswer"),
    "Answer":            ("text",),
    "Person":            ("name",),
    "ProfessionalService": ("name",),
    "LocalBusiness":     ("name", "address"),
    "Event":             ("name", "startDate"),
}


def _validate_schema_entity(entity: dict) -> list[dict]:
    """Returns list of {entity_type, missing_field} problems for one @type entity."""
    raw_type = entity.get("@type")
    if not raw_type:
        return []  # entities without @type can't be validated

    # @type may be a string or a list (e.g. ["Organization", "LocalBusiness"]).
    types = raw_type if isinstance(raw_type, list) else [raw_type]
    problems: list[dict] = []
    for t in types:
        if not isinstance(t, str):
            continue
        required = SCHEMA_REQUIRED_FIELDS.get(t)
        if not required:
            continue
        for field in required:
            value = entity.get(field)
            # "Missing" = key absent OR value is falsy/empty
            if value is None or value == "" or value == [] or value == {}:
                problems.append({"entity_type": t, "missing_field": field})
    return problems


def detect_indexability_issues(r: CrawlResult, page_type: str | None) -> list[dict]:
    issues = []
    robots = r.meta_robots or ""
    if "noindex" in robots or "none" in robots:
        # Only treat as a problem for pages that *should* be indexed.
        if page_type in ("home", "pillar", "service"):
            issues.append(_issue("noindex_meta", {
                "meta_robots": robots, "page_type": page_type,
            }))
    return issues


def detect_redirect_issues(r: CrawlResult) -> list[dict]:
    if len(r.redirect_chain) > REDIRECT_MAX_HOPS:
        return [_issue("redirect_chain_too_long", {
            "hops":  len(r.redirect_chain),
            "chain": r.redirect_chain,
        })]
    return []


# Shorthand
def _issue(itype: str, details: dict) -> dict:
    return {"issue_type": itype, "severity": SEVERITY[itype], "details": details}


def _origin(url: str | None) -> str | None:
    if not url:
        return None
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}".lower() if p.scheme and p.netloc else None


def run_all_detectors(r: CrawlResult, page_type: str | None) -> list[dict]:
    """Run every detector against a CrawlResult. Returns flat list of issues."""
    return (
        detect_title_issues(r)
        + detect_meta_description_issues(r)
        + detect_heading_issues(r)
        + detect_canonical_issues(r)
        + detect_alt_text_issues(r)
        + detect_thin_content(r, page_type)
        + detect_schema_issues(r)
        + detect_indexability_issues(r, page_type)
        + detect_redirect_issues(r)
    )


# ---------------------------------------------------------------------------
# Read phase
# ---------------------------------------------------------------------------

def load_due_pages(domain: str | None, page_types: tuple[str, ...],
                   cadence_days: int, force_all: bool) -> list[dict]:
    """Returns [{url, page_type}] for pages that need an audit."""
    sql = "SELECT url, page_type, last_audited FROM pages WHERE page_type = ANY(%s)"
    params: list = [list(page_types)]
    if domain:
        sql += " AND url LIKE %s"
        params.append(f"%{domain}%")
    sql += " ORDER BY url"
    rows = fetch_all(sql, params)

    if force_all:
        return [{"url": r["url"], "page_type": r["page_type"]} for r in rows]

    today_utc = datetime.now(timezone.utc)
    due: list[dict] = []
    for r in rows:
        last = r.get("last_audited")
        if last is None or (today_utc - last).days >= cadence_days:
            due.append({"url": r["url"], "page_type": r["page_type"]})
    return due


# ---------------------------------------------------------------------------
# Write phase
# ---------------------------------------------------------------------------

def find_open_issue(cur, url: str, issue_type: str) -> int | None:
    cur.execute(
        """
        SELECT id FROM technical_issues
         WHERE url = %s AND issue_type = %s AND date_resolved IS NULL
         LIMIT 1
        """,
        (url, issue_type),
    )
    row = cur.fetchone()
    if not row:
        return None
    return row[0] if not isinstance(row, dict) else row["id"]


def open_or_update_issue(cur, url: str, issue: dict) -> str:
    """
    Idempotent: if open issue exists for (url, issue_type), refresh its details
    (counts may have changed). Otherwise insert. Returns 'inserted' or 'updated'.
    """
    existing = find_open_issue(cur, url, issue["issue_type"])
    if existing is None:
        cur.execute(
            """
            INSERT INTO technical_issues (url, issue_type, severity, details)
            VALUES (%s, %s, %s, %s::jsonb)
            """,
            (url, issue["issue_type"], issue["severity"], json.dumps(issue["details"])),
        )
        return "inserted"
    cur.execute(
        "UPDATE technical_issues SET details = %s::jsonb WHERE id = %s",
        (json.dumps(issue["details"]), existing),
    )
    return "updated"


def detect_cross_page_duplicates(cur, urls_audited: set[str]) -> dict:
    """
    Post-pass: find pages with duplicate titles or meta descriptions within
    the same origin and open issues on each affected URL.

    Returns dict with: dup_title_n, dup_meta_n, inserted, updated, current_open.
    """
    out = {
        "dup_title_n":  0,
        "dup_meta_n":   0,
        "inserted":     0,
        "updated":      0,
        "current_open": set(),
    }

    if not urls_audited:
        return out

    # Duplicate titles, per origin, considering ALL pages (not just
    # newly-audited ones) so we catch dupes between a new audit and an
    # existing record.
    cur.execute(
        """
        WITH all_pages AS (
            SELECT url, title,
                   regexp_replace(url, '^(https?://[^/]+).*$', '\\1') AS origin
              FROM pages
             WHERE title IS NOT NULL
        ),
        dup_keys AS (
            SELECT origin, lower(title) AS title_key
              FROM all_pages
             GROUP BY origin, lower(title)
            HAVING count(*) > 1
        )
        SELECT a.url, a.title,
               (SELECT array_agg(a2.url ORDER BY a2.url)
                  FROM all_pages a2
                 WHERE a2.origin = a.origin
                   AND lower(a2.title) = lower(a.title)
                   AND a2.url <> a.url) AS other_urls
          FROM all_pages a
          JOIN dup_keys d
            ON d.origin = a.origin
           AND d.title_key = lower(a.title)
         WHERE a.url = ANY(%s)
        """,
        (list(urls_audited),),
    )
    for row in cur.fetchall():
        url      = row[0] if not isinstance(row, dict) else row["url"]
        title    = row[1] if not isinstance(row, dict) else row["title"]
        others   = row[2] if not isinstance(row, dict) else row["other_urls"]
        issue = {
            "issue_type": "duplicate_title",
            "severity":   SEVERITY["duplicate_title"],
            "details": {"title": title, "duplicate_count": 1 + len(others or []),
                        "other_urls": list(others or [])[:10]},
        }
        outcome = open_or_update_issue(cur, url, issue)
        out["inserted" if outcome == "inserted" else "updated"] += 1
        out["current_open"].add((url, "duplicate_title"))
        out["dup_title_n"] += 1

    # Duplicate meta descriptions, same logic.
    cur.execute(
        """
        WITH all_pages AS (
            SELECT url, meta_description,
                   regexp_replace(url, '^(https?://[^/]+).*$', '\\1') AS origin
              FROM pages
             WHERE meta_description IS NOT NULL
        ),
        dup_keys AS (
            SELECT origin, lower(meta_description) AS md_key
              FROM all_pages
             GROUP BY origin, lower(meta_description)
            HAVING count(*) > 1
        )
        SELECT a.url, a.meta_description,
               (SELECT array_agg(a2.url ORDER BY a2.url)
                  FROM all_pages a2
                 WHERE a2.origin = a.origin
                   AND lower(a2.meta_description) = lower(a.meta_description)
                   AND a2.url <> a.url) AS other_urls
          FROM all_pages a
          JOIN dup_keys d
            ON d.origin = a.origin
           AND d.md_key = lower(a.meta_description)
         WHERE a.url = ANY(%s)
        """,
        (list(urls_audited),),
    )
    for row in cur.fetchall():
        url    = row[0] if not isinstance(row, dict) else row["url"]
        md     = row[1] if not isinstance(row, dict) else row["meta_description"]
        others = row[2] if not isinstance(row, dict) else row["other_urls"]
        issue = {
            "issue_type": "duplicate_meta_description",
            "severity":   SEVERITY["duplicate_meta_description"],
            "details": {"meta_description": md, "duplicate_count": 1 + len(others or []),
                        "other_urls": list(others or [])[:10]},
        }
        outcome = open_or_update_issue(cur, url, issue)
        out["inserted" if outcome == "inserted" else "updated"] += 1
        out["current_open"].add((url, "duplicate_meta_description"))
        out["dup_meta_n"] += 1

    return out


def resolve_stale_issues(cur, urls_audited: set[str], current_open: set[tuple[str, str]]) -> int:
    """
    For each URL we audited this run, any open issue of a type we own that is
    NOT in current_open is now stale → mark resolved.
    """
    if not urls_audited:
        return 0
    cur.execute(
        """
        SELECT id, url, issue_type
          FROM technical_issues
         WHERE date_resolved IS NULL
           AND issue_type = ANY(%s)
           AND url = ANY(%s)
        """,
        (list(SEVERITY.keys()), list(urls_audited)),
    )
    resolved = 0
    for row in cur.fetchall():
        rid   = row[0] if not isinstance(row, dict) else row["id"]
        url   = row[1] if not isinstance(row, dict) else row["url"]
        itype = row[2] if not isinstance(row, dict) else row["issue_type"]
        if (url, itype) in current_open:
            continue
        cur.execute("UPDATE technical_issues SET date_resolved = now() WHERE id = %s", (rid,))
        resolved += 1
    return resolved


def update_page_audit_fields(cur, url: str, r: CrawlResult) -> None:
    """Persist audit-time metadata to `pages` (migration 006) + bump last_audited."""
    cur.execute(
        """
        UPDATE pages
           SET title            = %s,
               meta_description = %s,
               canonical_url    = %s,
               lang             = %s,
               word_count       = %s,
               last_audited     = now()
         WHERE url = %s
        """,
        (r.title, r.meta_description, r.canonical, r.lang, r.word_count, url),
    )


def update_last_audited(cur, url: str) -> None:
    """For pages that errored — only bump the timestamp."""
    cur.execute("UPDATE pages SET last_audited = now() WHERE url = %s", (url,))


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def crawl_pages_parallel(pages: list[dict], crawler: Crawler, workers: int) -> list[tuple[dict, CrawlResult]]:
    """Parallel fetch. Order of returns is non-deterministic."""
    results: list[tuple[dict, CrawlResult]] = []
    if not pages:
        return results

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(crawler.fetch, p["url"]): p for p in pages}
        for i, fut in enumerate(as_completed(futures), 1):
            page = futures[fut]
            if i % 25 == 0 or i == len(pages):
                logger.info("  fetched %d/%d", i, len(pages))
            try:
                results.append((page, fut.result()))
            except Exception as exc:
                # Construct a failed-result placeholder
                logger.error("Fetch failed for %s: %s", page["url"], exc)
                cr = CrawlResult(url=page["url"], error=str(exc))
                results.append((page, cr))
    return results


def write_results(results: list[tuple[dict, CrawlResult]], dry_run: bool) -> dict:
    counters = {
        "pages_audited":        0,
        "pages_with_errors":    0,
        "issues_inserted":      0,
        "issues_updated":       0,
        "issues_resolved":      0,
        "issue_counts_by_type": {},
    }
    current_open: set[tuple[str, str]] = set()
    urls_audited: set[str] = set()

    if dry_run:
        for page, r in results:
            urls_audited.add(page["url"])
            if r.error or not r.is_html:
                counters["pages_with_errors"] += 1
                continue
            counters["pages_audited"] += 1
            for issue in run_all_detectors(r, page["page_type"]):
                counters["issue_counts_by_type"][issue["issue_type"]] = \
                    counters["issue_counts_by_type"].get(issue["issue_type"], 0) + 1
        return counters

    with connection() as conn:
        cur = conn.cursor()
        for page, r in results:
            url = page["url"]
            urls_audited.add(url)

            if r.error or not r.is_html:
                counters["pages_with_errors"] += 1
                # Still mark last_audited so we don't keep retrying broken pages every run
                update_last_audited(cur, url)
                conn.commit()
                continue

            counters["pages_audited"] += 1
            page_issues = run_all_detectors(r, page["page_type"])
            for issue in page_issues:
                outcome = open_or_update_issue(cur, url, issue)
                if outcome == "inserted":
                    counters["issues_inserted"] += 1
                else:
                    counters["issues_updated"] += 1
                counters["issue_counts_by_type"][issue["issue_type"]] = \
                    counters["issue_counts_by_type"].get(issue["issue_type"], 0) + 1
                current_open.add((url, issue["issue_type"]))

            update_page_audit_fields(cur, url, r)
            conn.commit()

        # Cross-page post-pass: duplicate titles + meta descriptions. Runs
        # after every per-page row is persisted so we get a consistent view.
        cross = detect_cross_page_duplicates(cur, urls_audited)
        if cross["dup_title_n"]:
            counters["issue_counts_by_type"]["duplicate_title"] = cross["dup_title_n"]
        if cross["dup_meta_n"]:
            counters["issue_counts_by_type"]["duplicate_meta_description"] = cross["dup_meta_n"]
        counters["issues_inserted"] += cross["inserted"]
        counters["issues_updated"]  += cross["updated"]
        current_open |= cross["current_open"]
        conn.commit()

        counters["issues_resolved"] = resolve_stale_issues(cur, urls_audited, current_open)
        conn.commit()

    return counters


# ---------------------------------------------------------------------------
# Console summary
# ---------------------------------------------------------------------------

def print_summary(pages: list[dict], counters: dict,
                  results: list[tuple[dict, CrawlResult]], duration: float, dry_run: bool) -> None:
    print()
    print(f"  {'=' * 72}")
    print(f"   SITE AUDITOR — {date.today().isoformat()}{'  [DRY RUN]' if dry_run else ''}")
    print(f"  {'=' * 72}")
    print()
    print(f"  Pages requested:    {len(pages)}")
    print(f"  Pages audited (OK): {counters['pages_audited']}")
    print(f"  Pages with errors:  {counters['pages_with_errors']}")
    if not dry_run:
        print(f"  Issues inserted:    {counters['issues_inserted']}")
        print(f"  Issues updated:     {counters['issues_updated']}")
        print(f"  Issues resolved:    {counters['issues_resolved']}")
    print(f"  Duration:           {duration:.1f}s")
    print()

    if counters["issue_counts_by_type"]:
        print("  Issue counts by type (severity):")
        sorted_types = sorted(counters["issue_counts_by_type"].items(), key=lambda x: -x[1])
        for itype, n in sorted_types:
            sev = SEVERITY.get(itype, "?")
            print(f"    {sev:<10}  {itype:<30}  {n}")
        print()

    # Top-offending pages: how many issues each page has, sorted desc.
    offenders: dict[str, int] = {}
    for page, r in results:
        if r.error or not r.is_html:
            continue
        n = len(run_all_detectors(r, page["page_type"]))
        if n > 0:
            offenders[page["url"]] = n
    if offenders:
        top = sorted(offenders.items(), key=lambda x: -x[1])[:10]
        print("  Most-issue-laden pages (top 10):")
        for url, n in top:
            print(f"    {n:>2} issues  {url}")
        print()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(domain: str | None = None,
        page_types: tuple[str, ...] = DEFAULT_PAGE_TYPES,
        cadence_days: int = DEFAULT_CADENCE_DAYS,
        workers: int = DEFAULT_WORKERS,
        force_all: bool = False,
        dry_run: bool = False) -> dict:
    start = time.monotonic()

    pages = load_due_pages(domain, page_types, cadence_days, force_all)
    if not pages:
        msg = "No pages match the filter / due for audit"
        if domain: msg += f" (domain={domain})"
        msg += f" (page_types={list(page_types)})"
        logger.warning("%s", msg)
        return {"status": "skipped", "reason": "no pages"}

    logger.info("Auditing %d pages (workers=%d, cadence=%dd, force_all=%s)",
                len(pages), workers, cadence_days, force_all)

    crawler = Crawler()
    results = crawl_pages_parallel(pages, crawler, workers)
    counters = write_results(results, dry_run)
    duration = time.monotonic() - start

    if not dry_run:
        record_agent_run(
            agent_name=AGENT_NAME,
            status="success" if counters["pages_with_errors"] == 0 else "partial",
            records_processed=counters["pages_audited"],
            errors=[],
            duration_seconds=round(duration, 2),
            metadata={
                "run_date":            date.today().isoformat(),
                "domain":              domain,
                "page_types":          list(page_types),
                "cadence_days":        cadence_days,
                "force_all":           force_all,
                "pages_requested":     len(pages),
                "pages_audited":       counters["pages_audited"],
                "pages_with_errors":   counters["pages_with_errors"],
                "issues_inserted":     counters["issues_inserted"],
                "issues_updated":      counters["issues_updated"],
                "issues_resolved":     counters["issues_resolved"],
                "issue_counts_by_type": counters["issue_counts_by_type"],
            },
        )

    print_summary(pages, counters, results, duration, dry_run)
    return {
        "status":   "success" if counters["pages_with_errors"] == 0 else "partial",
        "counters": counters,
        "duration_seconds": round(duration, 2),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Damco Site Auditor")
    parser.add_argument("--domain", help="Restrict to one domain")
    parser.add_argument("--page-types", default=",".join(DEFAULT_PAGE_TYPES),
                        help=f"Comma-separated page types (default: {','.join(DEFAULT_PAGE_TYPES)})")
    parser.add_argument("--cadence", type=int, default=DEFAULT_CADENCE_DAYS,
                        help=f"Days between audits per page (default: {DEFAULT_CADENCE_DAYS})")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS,
                        help=f"Parallel workers (default: {DEFAULT_WORKERS})")
    parser.add_argument("--all", dest="force_all", action="store_true",
                        help="Force re-audit, ignore cadence")
    parser.add_argument("--dry-run", action="store_true",
                        help="Fetch + analyze but don't write to DB")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s  %(name)s  %(levelname)s  %(message)s",
    )

    page_types = tuple(p.strip() for p in args.page_types.split(",") if p.strip())

    run(domain=args.domain, page_types=page_types, cadence_days=args.cadence,
        workers=args.workers, force_all=args.force_all, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
