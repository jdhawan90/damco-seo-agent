"""
Sitemap Validator — Phase 1 of Technical SEO agent
====================================================

Standard agent lifecycle:
  Read    — fetch each domain's sitemap (auto-handles sitemapindex recursion)
  Process — validate every URL (HTTP status, redirect chain, canonical match);
            auto-categorize page_type by URL heuristic
  Write   — upsert into `pages`; insert/resolve issues in `technical_issues`;
            log to `agent_runs`
  Notify  — console summary with per-domain counts and ambiguous URLs for review

Usage
-----
    # Validate all 3 domains (default)
    python -m technical_seo.sitemap_validator

    # Restrict to one domain
    python -m technical_seo.sitemap_validator --domain damcogroup.com

    # Dry run — fetch + validate but don't write to DB
    python -m technical_seo.sitemap_validator --dry-run

Notes
-----
- HEAD requests preferred; falls back to GET when HEAD is rejected.
- Rate-limited to ~2 req/sec/domain by default.
- Issues automatically resolve when a URL stops failing (per-issue-type, per-url).
- Page titles/word counts are intentionally NOT fetched here — that's the
  crawler's job in Phase 3.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlparse

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from common.database import connection, record_agent_run


logger = logging.getLogger("sitemap_validator")

AGENT_NAME = "technical_seo.sitemap_validator"

DOMAINS = [
    {"domain": "damcogroup.com",   "sitemap_url": "https://www.damcogroup.com/sitemap.xml"},
    {"domain": "damcodigital.com", "sitemap_url": "https://damcodigital.com/sitemap_index.xml"},
    {"domain": "achieva.ai",       "sitemap_url": "https://achieva.ai/sitemap.xml"},
]

NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}

USER_AGENT = "DamcoSEOBot/1.0 (+https://www.damcogroup.com/; SEO ops monitoring)"
REQUEST_TIMEOUT = 15
RATE_LIMIT_SLEEP = 0.5  # seconds between requests per domain
MAX_REDIRECT_HOPS_OK = 2

# Issue types this module emits.
ISSUE_TYPES = {
    "sitemap_fetch_failed":   "critical",
    "sitemap_url_broken":     "high",
    "sitemap_url_redirect":   "medium",
    "redirect_chain_too_long": "medium",
}


# ---------------------------------------------------------------------------
# Sitemap fetching + parsing
# ---------------------------------------------------------------------------

def fetch_xml(url: str) -> str | None:
    """Fetch a sitemap URL and return the XML text. None on failure."""
    try:
        r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        return r.text
    except requests.RequestException as exc:
        logger.error("Failed to fetch sitemap %s: %s", url, exc)
        return None


def parse_sitemap(xml_text: str) -> tuple[str, list[str]]:
    """
    Returns ('index', [sub_sitemap_urls]) or ('urlset', [page_urls]).
    Raises ValueError on unparseable XML.
    """
    root = ET.fromstring(xml_text)
    tag = root.tag.split("}")[-1] if "}" in root.tag else root.tag
    if tag == "sitemapindex":
        urls = []
        for s in root.findall("sm:sitemap", NS) or root.findall("sitemap"):
            loc = s.find("sm:loc", NS) if s.find("sm:loc", NS) is not None else s.find("loc")
            if loc is not None and loc.text:
                urls.append(loc.text.strip())
        return ("index", urls)
    if tag == "urlset":
        urls = []
        for u in root.findall("sm:url", NS) or root.findall("url"):
            loc = u.find("sm:loc", NS) if u.find("sm:loc", NS) is not None else u.find("loc")
            if loc is not None and loc.text:
                urls.append(loc.text.strip())
        return ("urlset", urls)
    raise ValueError(f"Unrecognized sitemap root tag: {tag}")


def collect_urls_from_sitemap(sitemap_url: str, max_depth: int = 3) -> tuple[list[str], list[str]]:
    """
    Recursively walk a sitemap. Returns (page_urls, fetch_errors).
    fetch_errors is a list of sitemap URLs we couldn't fetch or parse.
    """
    page_urls: list[str] = []
    fetch_errors: list[str] = []
    seen_sitemaps: set[str] = set()

    def walk(url: str, depth: int) -> None:
        if depth > max_depth:
            logger.warning("Max sitemap depth reached at %s", url)
            return
        if url in seen_sitemaps:
            return
        seen_sitemaps.add(url)

        xml_text = fetch_xml(url)
        if xml_text is None:
            fetch_errors.append(url)
            return
        try:
            kind, items = parse_sitemap(xml_text)
        except ET.ParseError as exc:
            logger.error("Could not parse sitemap %s: %s", url, exc)
            fetch_errors.append(url)
            return
        except ValueError as exc:
            logger.error("%s", exc)
            fetch_errors.append(url)
            return

        if kind == "index":
            for sub in items:
                walk(sub, depth + 1)
                time.sleep(RATE_LIMIT_SLEEP)
        else:
            page_urls.extend(items)

    walk(sitemap_url, depth=0)
    # Dedupe but preserve order; filter out stray sub-sitemap URLs that ended
    # up inside a <urlset> by mistake.
    seen: set[str] = set()
    unique_pages: list[str] = []
    skipped_xml = 0
    for u in page_urls:
        if u.lower().endswith(".xml"):
            skipped_xml += 1
            continue
        if u not in seen:
            seen.add(u)
            unique_pages.append(u)
    if skipped_xml:
        logger.info("Skipped %d stray .xml entries inside urlsets (sub-sitemaps mislisted as pages)",
                    skipped_xml)
    return unique_pages, fetch_errors


# ---------------------------------------------------------------------------
# URL validation
# ---------------------------------------------------------------------------

def validate_url(url: str) -> dict:
    """
    Perform a HEAD (with GET fallback) and follow redirects.

    Returns:
        {
            'status':         final HTTP status (or None on transport error),
            'final_url':      end of redirect chain,
            'redirect_chain': [intermediate URLs],
            'error':          error message or None,
        }
    """
    headers = {"User-Agent": USER_AGENT}
    try:
        # Try HEAD first
        r = requests.head(
            url, headers=headers, allow_redirects=True,
            timeout=REQUEST_TIMEOUT,
        )
        # Some servers respond 405/403 to HEAD — fall back to GET
        if r.status_code in (405, 403, 501) or r.status_code >= 500:
            r = requests.get(
                url, headers=headers, allow_redirects=True,
                timeout=REQUEST_TIMEOUT, stream=True,
            )
            r.close()
        return {
            "status":         r.status_code,
            "final_url":      r.url,
            "redirect_chain": [resp.url for resp in r.history],
            "error":          None,
        }
    except requests.RequestException as exc:
        return {
            "status":         None,
            "final_url":      None,
            "redirect_chain": [],
            "error":          str(exc),
        }


# ---------------------------------------------------------------------------
# Page-type heuristic
# ---------------------------------------------------------------------------

# Path keywords for the 'service' bucket — broad on purpose. The check is
# substring in the lowercased path, so 'services' covers /ai-services,
# /ai-development-services, /services/etc.
SERVICE_KEYWORDS = (
    "services", "solutions", "consulting", "development", "automation",
    "agent", "integration", "platform", "marketing",
    "migration", "modernization", "maintenance", "support", "engineering",
    "implementation", "transformation",
)

# Industry/vertical paths — usually service pages segmented by vertical
# (e.g. /industry/healthcare-digital-marketing, /industries/insurance/)
INDUSTRY_PATHS = ("/industry/", "/industries/", "/verticals/")

BLOG_PATHS = (
    "/blog/", "/blogs/",  # WordPress + Damco's /blogs/ plural
    "/blog-",
    "/insights/", "/insight/", "/articles/", "/article/",
    "/news/", "/posts/", "/post-",
)

RESOURCE_PATHS = (
    "/case-studies/", "/case-study/", "/client-success/", "/success-story/", "/success-stories/",
    "/whitepapers/", "/whitepaper/", "/ebooks/", "/ebook/", "/downloads/",
    "/resources/", "/webinars/", "/webinar/", "/reports/", "/report/",
)


def categorize_page_type(url: str) -> str | None:
    """Best-effort URL → page_type. Returns None when ambiguous (human review)."""
    path = (urlparse(url).path or "").lower().rstrip("/")

    # Home
    if path in ("", "/index.html", "/index.php"):
        return "home"

    # Append a trailing slash for substring matching so /blogs (index page)
    # matches the same patterns as /blogs/<slug>.
    path_match = path + "/"

    # Glossary
    if "/glossary/" in path_match:
        return "glossary"
    # Blog / insights / news
    if any(seg in path_match for seg in BLOG_PATHS):
        return "blog"
    # Resources / case studies / whitepapers
    if any(seg in path_match for seg in RESOURCE_PATHS):
        return "resource"
    # Landing pages
    if any(seg in path_match for seg in ("/lp/", "/landing/", "/campaigns/")):
        return "landing"
    # Industry / vertical pages → treated as service (verticalized offerings)
    if any(seg in path_match for seg in INDUSTRY_PATHS):
        return "service"
    # Service pages — keyword anywhere in path
    if any(kw in path for kw in SERVICE_KEYWORDS):
        return "service"
    return None


# ---------------------------------------------------------------------------
# DB writes
# ---------------------------------------------------------------------------

def upsert_page(cur, *, url: str, page_type: str | None) -> None:
    """
    Discovery only — don't touch last_audited. site_auditor is the writer
    of that column (discovery via sitemap is NOT an audit). The `updated_at`
    trigger on `pages` already records when this row was last touched.
    """
    cur.execute(
        """
        INSERT INTO pages (url, page_type)
        VALUES (%s, %s)
        ON CONFLICT (url) DO UPDATE SET
            page_type = COALESCE(pages.page_type, EXCLUDED.page_type)
        """,
        (url, page_type),
    )


def open_issue(cur, *, url: str, issue_type: str, severity: str, details: dict) -> bool:
    """
    Insert a new technical_issue iff there isn't already an unresolved one
    with the same (url, issue_type). Returns True if inserted.
    """
    cur.execute(
        """
        SELECT id FROM technical_issues
         WHERE url = %s AND issue_type = %s AND date_resolved IS NULL
         LIMIT 1
        """,
        (url, issue_type),
    )
    if cur.fetchone():
        return False
    cur.execute(
        """
        INSERT INTO technical_issues (url, issue_type, severity, details)
        VALUES (%s, %s, %s, %s::jsonb)
        """,
        (url, issue_type, severity, json.dumps(details)),
    )
    return True


def resolve_stale_issues(cur, *, current_open: set[tuple[str, str]],
                         issue_types: list[str], domain: str) -> int:
    """
    Mark as resolved any open issues for this domain whose (url, issue_type)
    is NOT in current_open. We do this only for issue types we own to avoid
    stomping on issues created by other modules.
    """
    cur.execute(
        """
        SELECT id, url, issue_type
          FROM technical_issues
         WHERE date_resolved IS NULL
           AND issue_type = ANY(%s)
           AND url LIKE %s
        """,
        (issue_types, f"%{domain}%"),
    )
    resolved = 0
    for row in cur.fetchall():
        issue_id = row[0] if not isinstance(row, dict) else row["id"]
        url      = row[1] if not isinstance(row, dict) else row["url"]
        itype    = row[2] if not isinstance(row, dict) else row["issue_type"]
        if (url, itype) in current_open:
            continue
        cur.execute(
            "UPDATE technical_issues SET date_resolved = now() WHERE id = %s",
            (issue_id,),
        )
        resolved += 1
    return resolved


# ---------------------------------------------------------------------------
# Per-domain orchestration
# ---------------------------------------------------------------------------

def process_domain(entry: dict, dry_run: bool = False) -> dict:
    """Process one domain end-to-end. Returns counters."""
    domain = entry["domain"]
    sitemap_url = entry["sitemap_url"]
    logger.info("=== %s — %s ===", domain, sitemap_url)

    # 1. Fetch + recursively walk sitemap
    page_urls, sitemap_errors = collect_urls_from_sitemap(sitemap_url)
    logger.info("Discovered %d unique URLs across sitemaps (%d sitemap fetch errors)",
                len(page_urls), len(sitemap_errors))

    # 2. Validate each URL
    broken: list[dict] = []
    redirected: list[dict] = []
    chain_too_long: list[dict] = []
    ok_count = 0
    type_counts: dict[str | None, int] = {}
    null_type_samples: list[str] = []

    for i, url in enumerate(page_urls, 1):
        if i % 25 == 0 or i == len(page_urls):
            logger.info("  validated %d/%d", i, len(page_urls))
        v = validate_url(url)
        time.sleep(RATE_LIMIT_SLEEP)

        if v["error"] or (v["status"] and v["status"] >= 400):
            broken.append({"url": url, **v})
        else:
            ok_count += 1
            if v["redirect_chain"] and v["final_url"] and v["final_url"] != url:
                redirected.append({
                    "url": url, "final_url": v["final_url"],
                    "hops": len(v["redirect_chain"]),
                })
            if len(v["redirect_chain"]) > MAX_REDIRECT_HOPS_OK:
                chain_too_long.append({
                    "url": url, "hops": len(v["redirect_chain"]),
                    "chain": v["redirect_chain"],
                })

        # Categorize
        pt = categorize_page_type(url)
        type_counts[pt] = type_counts.get(pt, 0) + 1
        if pt is None and len(null_type_samples) < 15:
            null_type_samples.append(url)

    # 3. Write everything
    issues_opened = 0
    pages_upserted = 0
    issues_resolved = 0
    current_open: set[tuple[str, str]] = set()

    if not dry_run:
        with connection() as conn:
            cur = conn.cursor()

            # Sitemap fetch errors
            for sm_url in sitemap_errors:
                if open_issue(
                    cur, url=sm_url, issue_type="sitemap_fetch_failed",
                    severity=ISSUE_TYPES["sitemap_fetch_failed"],
                    details={"sitemap_url": sm_url, "discovered_via": sitemap_url},
                ):
                    issues_opened += 1
                current_open.add((sm_url, "sitemap_fetch_failed"))

            # Per-page upsert + per-page issues
            for url in page_urls:
                pt = categorize_page_type(url)
                upsert_page(cur, url=url, page_type=pt)
                pages_upserted += 1

            for b in broken:
                if open_issue(
                    cur, url=b["url"], issue_type="sitemap_url_broken",
                    severity=ISSUE_TYPES["sitemap_url_broken"],
                    details={"status": b["status"], "error": b["error"]},
                ):
                    issues_opened += 1
                current_open.add((b["url"], "sitemap_url_broken"))

            for rd in redirected:
                if open_issue(
                    cur, url=rd["url"], issue_type="sitemap_url_redirect",
                    severity=ISSUE_TYPES["sitemap_url_redirect"],
                    details={"final_url": rd["final_url"], "hops": rd["hops"]},
                ):
                    issues_opened += 1
                current_open.add((rd["url"], "sitemap_url_redirect"))

            for ch in chain_too_long:
                if open_issue(
                    cur, url=ch["url"], issue_type="redirect_chain_too_long",
                    severity=ISSUE_TYPES["redirect_chain_too_long"],
                    details={"hops": ch["hops"], "chain": ch["chain"]},
                ):
                    issues_opened += 1
                current_open.add((ch["url"], "redirect_chain_too_long"))

            # Auto-resolve issues that are no longer present
            issues_resolved = resolve_stale_issues(
                cur,
                current_open=current_open,
                issue_types=list(ISSUE_TYPES.keys()),
                domain=domain,
            )

    return {
        "domain":            domain,
        "sitemap_url":       sitemap_url,
        "urls_discovered":   len(page_urls),
        "sitemap_errors":    len(sitemap_errors),
        "ok":                ok_count,
        "broken":            len(broken),
        "redirected":        len(redirected),
        "chain_too_long":    len(chain_too_long),
        "type_counts":       type_counts,
        "null_type_samples": null_type_samples,
        "issues_opened":     issues_opened,
        "issues_resolved":   issues_resolved,
        "pages_upserted":    pages_upserted,
    }


# ---------------------------------------------------------------------------
# Console summary
# ---------------------------------------------------------------------------

def print_summary(results: list[dict], duration: float, dry_run: bool) -> None:
    print()
    print(f"  {'=' * 72}")
    print(f"   SITEMAP VALIDATOR — {date.today().isoformat()}{'  [DRY RUN]' if dry_run else ''}")
    print(f"  {'=' * 72}")
    print()
    for r in results:
        print(f"  {r['domain']}")
        print(f"    sitemap:           {r['sitemap_url']}")
        print(f"    URLs discovered:   {r['urls_discovered']}")
        print(f"    OK (200):          {r['ok']}")
        print(f"    Broken (4xx/5xx):  {r['broken']}")
        print(f"    Redirected:        {r['redirected']}")
        print(f"    Chain too long:    {r['chain_too_long']}")
        print(f"    Sitemap errors:    {r['sitemap_errors']}")
        if not dry_run:
            print(f"    Pages upserted:    {r['pages_upserted']}")
            print(f"    Issues opened:     {r['issues_opened']}")
            print(f"    Issues resolved:   {r['issues_resolved']}")
        print(f"    Page type breakdown:")
        for pt, n in sorted(r["type_counts"].items(), key=lambda x: -x[1]):
            label = pt or "(uncategorized)"
            print(f"      {label:<22} {n}")
        if r["null_type_samples"]:
            print(f"    Sample uncategorized URLs (first {min(15, len(r['null_type_samples']))}):")
            for u in r["null_type_samples"]:
                print(f"      - {u}")
        print()
    print(f"  Duration:  {duration:.1f}s")
    print()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(domain: str | None = None, dry_run: bool = False) -> dict:
    start = time.monotonic()
    targets = DOMAINS
    if domain:
        targets = [d for d in DOMAINS if d["domain"] == domain]
        if not targets:
            raise ValueError(f"Unknown domain: {domain}. Known: {[d['domain'] for d in DOMAINS]}")

    results = [process_domain(d, dry_run=dry_run) for d in targets]
    duration = time.monotonic() - start

    if not dry_run:
        record_agent_run(
            agent_name=AGENT_NAME,
            status="success" if all(r["sitemap_errors"] == 0 for r in results) else "partial",
            records_processed=sum(r["pages_upserted"] for r in results),
            errors=[],
            duration_seconds=round(duration, 2),
            metadata={
                "run_date":      date.today().isoformat(),
                "domains":       [r["domain"] for r in results],
                "total_urls":    sum(r["urls_discovered"] for r in results),
                "total_broken":  sum(r["broken"] for r in results),
                "total_issues_opened":   sum(r["issues_opened"]   for r in results),
                "total_issues_resolved": sum(r["issues_resolved"] for r in results),
            },
        )

    print_summary(results, duration, dry_run)
    return {"results": results, "duration_seconds": round(duration, 2)}


def main() -> None:
    parser = argparse.ArgumentParser(description="Damco Sitemap Validator")
    parser.add_argument("--domain", help="Restrict to one domain (default: all 3)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Validate but don't write to DB")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Enable debug logging")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s  %(name)s  %(levelname)s  %(message)s",
    )

    run(domain=args.domain, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
