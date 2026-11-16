"""
Phase 1 — crawl every owned-domain page listed in the tenant sitemaps and
persist its outgoing links into `internal_links`.

Why so simple:
  The crawler primitive (common.connectors.crawler.Crawler) already extracts
  {href, anchor, rel, is_internal} per <a> tag on each page. This module just
  (a) enumerates the URLs to crawl, (b) calls the crawler, (c) upserts the
  rows. No parsing lives here.

What gets stored:
  Every link, internal AND external, from every crawled page. The
  detectors only read the internal subset, but keeping outbound links too
  costs almost nothing and lets a future "outbound audit" detector run
  without a recrawl.

Re-seen semantics:
  first_seen is pinned on insert. last_seen is bumped to today on every
  observation. A link that disappears from the source page simply stops
  having its last_seen advance — the stale window is a real signal, not
  a deletion event.
"""
from __future__ import annotations

import logging
import time
from collections import Counter
from dataclasses import dataclass
from datetime import date
from typing import Iterable
from urllib.parse import urlparse

from common.config import settings
from common.connectors.crawler import Crawler, CrawlResult
from common.database import connection, record_agent_run
from common.sitemap import collect_urls_from_sitemap
from common.tenant import profile

logger = logging.getLogger("content_operations.internal_linker.crawler")

AGENT_NAME = "content_operations.internal_linker.crawl"

# ---------------------------------------------------------------------------
# URL enumeration
# ---------------------------------------------------------------------------

def _enumerate_owned_urls() -> list[str]:
    """Walk every configured sitemap (including extras) and dedupe URLs."""
    p = profile()
    seen: set[str] = set()
    ordered: list[str] = []
    for row in p.domain_rows:
        for sm in [row.get("sitemap_url")] + list(row.get("extra_sitemaps") or []):
            if not sm:
                continue
            try:
                urls, _ = collect_urls_from_sitemap(sm, max_depth=3)
            except Exception as exc:
                logger.warning("sitemap %s: failed to collect URLs (%s)", sm, exc)
                continue
            for u in urls:
                if u not in seen:
                    seen.add(u)
                    ordered.append(u)
    return ordered


def _is_crawlable(url: str) -> bool:
    """Skip non-html by extension to keep the queue lean and the fetcher honest."""
    path = urlparse(url).path.lower()
    skip_ext = (".pdf", ".zip", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
                ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".ico",
                ".mp4", ".mp3", ".webm", ".css", ".js", ".xml", ".json")
    return not any(path.endswith(ext) for ext in skip_ext)


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

UPSERT_LINK_SQL = """
    INSERT INTO internal_links
        (source_url, target_url, anchor_text, rel, is_internal,
         first_seen, last_seen, date_crawled)
    VALUES (%s, %s, %s, %s, %s,
            CURRENT_DATE, CURRENT_DATE, CURRENT_TIMESTAMP)
    ON CONFLICT (source_url, target_url, anchor_text)
    DO UPDATE SET
        last_seen    = CURRENT_DATE,
        date_crawled = CURRENT_TIMESTAMP,
        rel          = EXCLUDED.rel,
        is_internal  = EXCLUDED.is_internal
"""

def _persist_links(cur, from_url: str, links: list[dict]) -> int:
    """
    Insert one row per unique (to_url, anchor_text). Multiple identical anchors
    to the same destination on the same page are collapsed — otherwise the
    unique constraint rejects the second one.
    """
    written = 0
    seen_in_page: set[tuple[str, str]] = set()
    for link in links:
        href = (link.get("href") or "").strip()
        if not href:
            continue
        anchor = link.get("anchor") or None
        key = (href, anchor or "")
        if key in seen_in_page:
            continue
        seen_in_page.add(key)
        cur.execute(UPSERT_LINK_SQL, (
            from_url, href, anchor, link.get("rel"),
            bool(link.get("is_internal")),
        ))
        written += 1
    return written


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

@dataclass
class CrawlStats:
    attempted: int = 0
    fetched: int = 0
    skipped_non_html: int = 0
    errors: int = 0
    links_written: int = 0
    by_status: Counter = None  # populated in run()


def run(max_pages: int | None = None,
        rate_limit_sec: float = 1.0,
        respect_robots: bool = True) -> dict:
    """
    Crawl every owned-domain URL and upsert its outgoing links.

    Parameters
    ----------
    max_pages
        Cap crawl to this many URLs for ad-hoc testing. None = everything.
    rate_limit_sec
        Per-host delay between requests. 1.0s is polite; tune down only if
        you've asked ops first.
    respect_robots
        Honor robots.txt. Leave True in production.
    """
    start = time.monotonic()
    stats = CrawlStats(by_status=Counter())
    api_errors: list[str] = []

    urls = _enumerate_owned_urls()
    logger.info("enumerated %d URLs from owned-domain sitemaps", len(urls))

    crawl_queue = [u for u in urls if _is_crawlable(u)]
    stats.skipped_non_html = len(urls) - len(crawl_queue)
    if max_pages is not None:
        crawl_queue = crawl_queue[:max_pages]
    stats.attempted = len(crawl_queue)
    logger.info("will crawl %d pages (skipped %d non-html, max_pages=%s)",
                len(crawl_queue), stats.skipped_non_html, max_pages)

    p = profile()
    crawler = Crawler(
        user_agent=p.user_agent,
        timeout_sec=20,
        rate_limit_sec=rate_limit_sec,
        respect_robots=respect_robots,
    )

    with connection() as conn:
        cur = conn.cursor()
        for i, url in enumerate(crawl_queue, 1):
            try:
                res: CrawlResult = crawler.fetch(url)
            except Exception as exc:
                stats.errors += 1
                api_errors.append(f"{url}: {exc}")
                logger.warning("fetch failed: %s (%s)", url, exc)
                continue

            stats.by_status[res.status or 0] += 1
            if not res.is_html or res.error:
                continue
            stats.fetched += 1
            try:
                written = _persist_links(cur, res.final_url or url, res.links or [])
                stats.links_written += written
                if i % 50 == 0:
                    conn.commit()
                    logger.info("progress: %d/%d pages crawled, %d links written",
                                i, stats.attempted, stats.links_written)
            except Exception as exc:
                conn.rollback()
                stats.errors += 1
                api_errors.append(f"{url}: persist failed: {exc}")
                logger.exception("persist failed for %s", url)
                continue
        conn.commit()

    duration = time.monotonic() - start
    record_agent_run(
        agent_name=AGENT_NAME,
        status="success" if not api_errors else "partial",
        records_processed=stats.links_written,
        errors=api_errors or None,
        duration_seconds=round(duration, 2),
        metadata={
            "run_date":         date.today().isoformat(),
            "urls_enumerated":  len(urls),
            "pages_attempted":  stats.attempted,
            "pages_fetched":    stats.fetched,
            "skipped_non_html": stats.skipped_non_html,
            "errors":           stats.errors,
            "links_written":    stats.links_written,
            "status_codes":     dict(stats.by_status),
            "rate_limit_sec":   rate_limit_sec,
        },
    )

    logger.info(
        "crawl done in %.1fs: pages %d/%d fetched, %d links written, %d errors",
        duration, stats.fetched, stats.attempted, stats.links_written, stats.errors,
    )
    return {
        "status": "success" if not api_errors else "partial",
        "pages_fetched": stats.fetched,
        "pages_attempted": stats.attempted,
        "links_written": stats.links_written,
        "errors": stats.errors,
        "duration_seconds": round(duration, 2),
    }
