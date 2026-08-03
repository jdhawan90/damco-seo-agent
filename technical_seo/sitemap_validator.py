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
    # Validate every domain in the tenant profile (default)
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
from common.tenant import profile, strip_www


logger = logging.getLogger("sitemap_validator")

AGENT_NAME = "technical_seo.sitemap_validator"

# Sitemap parsing + walking lives in common/sitemap.py so the
# competitive_intelligence.content_monitor can reuse it without crossing
# agent boundaries. fetch_xml / parse_sitemap / collect_urls_from_sitemap
# are imported from there now.
from common.sitemap import (
    fetch_xml,
    parse_sitemap,
    collect_urls_from_sitemap,
    discover_sitemap_urls,
    user_agent,
    REQUEST_TIMEOUT,
)

RATE_LIMIT_SLEEP = 0.5  # seconds between requests per domain (HEAD/GET validation)
MAX_REDIRECT_HOPS_OK = 2

# Issue types this module emits.
ISSUE_TYPES = {
    "sitemap_fetch_failed":   "critical",
    "sitemap_url_broken":     "high",
    "sitemap_url_redirect":   "medium",
    "redirect_chain_too_long": "medium",
}


# Sitemap-fetching utilities live in common/sitemap.py — see top of file
# for the import.


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
    headers = {"User-Agent": user_agent()}
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

def categorize_page_type(url: str) -> str | None:
    """Best-effort URL → page_type. Returns None when ambiguous (human review)."""
    p = profile()
    path = (urlparse(url).path or "").lower().rstrip("/")

    # Home
    if path in ("", "/index.html", "/index.php"):
        return "home"

    # Append a trailing slash for substring matching so /blogs (index page)
    # matches the same patterns as /blogs/<slug>.
    path_match = path + "/"

    # Path map, longest segment first, so /industries_success/ (resource) wins
    # over /industries/ (service). A NULL label means the segment is recognised
    # but deliberately out of scope — the WordPress taxonomy archives, which are
    # auto-generated index pages with no unique content. Those return None here
    # rather than falling through to the service-keyword check below.
    for segment, page_type in p.vocab_labeled("url_path_map"):
        if segment in path_match:
            return page_type

    # Service pages — keyword anywhere in the path. Broad on purpose: the check
    # is a substring of the lowercased path, so 'services' covers /ai-services,
    # /ai-development-services and /services/etc.
    if any(kw in path for kw in p.vocab("service_keywords")):
        return "service"
    return None


# ---------------------------------------------------------------------------
# LLM fallback for URLs the rules cannot place
#
# The rules above are a hand-rolled classifier with an escape hatch: they
# return None and the module dumps the uncategorized URLs for a human. That
# is the exact case the repo's "rule-based first, LLM second" principle
# sanctions a model for — genuinely ambiguous classification.
#
# It is also the portability fix. A new client currently needs someone to
# hand-edit `service_keywords` for their vocabulary; with this, the rules
# handle what they can and the model covers the rest.
#
# Three constraints make the cost bounded:
#   * only URLs that returned None reach here
#   * one batched call per run, not one per URL
#   * the answer persists in `pages.page_type` for every downstream consumer
#
# Note what is NOT true: a URL the rules cannot place is re-sent on every run,
# because the unknown set is derived from `categorize_page_type()` and this
# module never reads `pages.page_type` back. The write is stable — `upsert_page`
# COALESCEs, so the first classification wins and a later low-confidence answer
# cannot overwrite it — but the call is repeated. Cost is one cheap batched
# call per domain per run, which is why this is documented rather than fixed.
# ---------------------------------------------------------------------------

VALID_PAGE_TYPES = ("home", "pillar", "service", "blog", "resource",
                    "glossary", "landing")
MAX_LLM_CLASSIFY = 150


def classify_unknown_urls(urls: list[str]) -> dict[str, str]:
    """
    Ask the model to place URLs the rules could not. Returns {url: page_type}
    for confident answers only; anything unrecognised is dropped so it stays
    NULL for human review rather than being guessed into the wrong bucket.

    Returns {} on any failure — the caller keeps the NULLs and the run is
    otherwise unchanged.
    """
    if not urls:
        return {}

    batch = urls[:MAX_LLM_CLASSIFY]
    listing = "\n".join(f"  {u}" for u in batch)
    prompt = (
        "Classify each URL into exactly one page type, judging from the path.\n\n"
        f"Types: {', '.join(VALID_PAGE_TYPES)}\n"
        "  home      the site root\n"
        "  pillar    a broad hub page that links to many narrower pages\n"
        "  service   a page selling a specific service or capability\n"
        "  blog      a dated article or post\n"
        "  resource  a case study, whitepaper, ebook or webinar\n"
        "  glossary  a definition of a single term\n"
        "  landing   a campaign or paid-traffic page\n\n"
        f"URLS:\n{listing}\n\n"
        'Return JSON only: {"<url>": "<type>", ...}. Include a URL only if you '
        "are confident; omit the ones you are not. Do not invent types."
    )

    from common.llm import call_claude_json

    value, usage, error = call_claude_json(
        prompt, fallback={}, tier="cheap", max_tokens=4000)
    if error:
        logger.info("Page-type fallback unavailable (%s) — %d URL(s) stay "
                    "uncategorized for human review", error, len(batch))
        return {}
    if not isinstance(value, dict):
        return {}

    out: dict[str, str] = {}
    for url, page_type in value.items():
        pt = str(page_type or "").strip().lower()
        if url in set(batch) and pt in VALID_PAGE_TYPES:
            out[url] = pt
    logger.info("Page-type fallback classified %d of %d unknown URL(s)",
                len(out), len(batch))
    return out


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

def load_domains(domain: str | None = None) -> list[dict]:
    """
    Target domains from the tenant profile, as {domain, sitemap_url}.

    A profile row with no sitemap_url gets one discovered at run time (the
    conventional locations plus robots.txt) instead of being skipped.
    """
    rows = profile().domain_rows
    if domain:
        wanted = strip_www(domain)
        rows = [r for r in rows if strip_www(r["domain"]) == wanted]
        if not rows:
            known = [r["domain"] for r in profile().domain_rows]
            raise ValueError(f"Unknown domain: {domain}. Known: {known}")

    targets: list[dict] = []
    for row in rows:
        sitemap_url = row["sitemap_url"]
        if not sitemap_url:
            found = discover_sitemap_urls(row["domain"])
            if not found:
                logger.error("No sitemap found for %s — skipping", row["domain"])
                continue
            sitemap_url = found[0]
            logger.info("Discovered sitemap for %s: %s", row["domain"], sitemap_url)

        # extra_sitemaps: sitemaps the root index does not reference.
        # damcogroup.com's /insurance/ section is a separate WordPress
        # multisite generating its own sitemap that nothing links to, so
        # walking sitemap_url alone missed 47 product pages and, with them,
        # the target of 314 tracked keywords. See migration 023.
        urls = [sitemap_url] + [s for s in (row.get("extra_sitemaps") or [])
                                if s and s != sitemap_url]
        if len(urls) > 1:
            logger.info("%s: %d sitemap(s) — %s", row["domain"], len(urls),
                        ", ".join(u.split("/", 3)[-1] for u in urls))
        targets.append({"domain": row["domain"], "sitemap_url": sitemap_url,
                        "sitemap_urls": urls})
    return targets


def process_domain(entry: dict, dry_run: bool = False,
                   use_llm_fallback: bool = True) -> dict:
    """Process one domain end-to-end. Returns counters."""
    domain = entry["domain"]
    sitemap_url = entry["sitemap_url"]
    sitemap_urls = entry.get("sitemap_urls") or [sitemap_url]
    logger.info("=== %s — %s ===", domain, sitemap_url)

    # 1. Fetch + recursively walk every declared sitemap for this domain.
    #    Union, de-duplicated: a page listed in two sitemaps is one page.
    page_urls: list[str] = []
    sitemap_errors: list = []
    seen_urls: set[str] = set()
    for sm in sitemap_urls:
        urls, errs = collect_urls_from_sitemap(sm)
        fresh = [u for u in urls if u not in seen_urls]
        seen_urls.update(fresh)
        page_urls.extend(fresh)
        sitemap_errors.extend(errs)
        if len(sitemap_urls) > 1:
            logger.info("  %s -> %d urls (%d new)", sm, len(urls), len(fresh))
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

    # Categorize once, up front, so the counts and the upsert below cannot
    # disagree — categorize_page_type used to be called separately in each
    # place, which would now mean paying for the LLM fallback twice.
    page_types: dict[str, str | None] = {u: categorize_page_type(u) for u in page_urls}

    unknown = [u for u, pt in page_types.items() if pt is None]
    llm_classified = 0
    if unknown and use_llm_fallback:
        resolved = classify_unknown_urls(unknown)
        page_types.update(resolved)
        llm_classified = len(resolved)

    for url in page_urls:
        pt = page_types[url]
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
                upsert_page(cur, url=url, page_type=page_types[url])
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
        "llm_classified":    llm_classified,
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
        if r.get("llm_classified"):
            print(f"      (of which {r['llm_classified']} placed by the "
                  f"page-type fallback, not the rules)")
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

def run(domain: str | None = None, dry_run: bool = False,
        use_llm_fallback: bool = True) -> dict:
    start = time.monotonic()
    targets = load_domains(domain)

    results = [process_domain(d, dry_run=dry_run,
                              use_llm_fallback=use_llm_fallback) for d in targets]
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
    parser = argparse.ArgumentParser(
        description=f"{profile().brand_name} Sitemap Validator")
    parser.add_argument("--domain",
                        help="Restrict to one domain (default: all owned domains)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Validate but don't write to DB")
    parser.add_argument("--no-llm", action="store_true",
                        help="Skip the page-type fallback; leave unresolvable "
                             "URLs uncategorized for human review")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Enable debug logging")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s  %(name)s  %(levelname)s  %(message)s",
    )

    run(domain=args.domain, dry_run=args.dry_run,
        use_llm_fallback=not args.no_llm)


if __name__ == "__main__":
    main()
