"""
Backlink Tracker — Phase 1 module of Off-Page & Links
======================================================

Monthly (or on-demand) refresh of Damco's backlink inventory from two
sources:
  - DataForSEO Backlinks API     (authoritative, paid subscription)
  - Google Search Console        (Google's own view of Damco's backlinks)

Both feed into the shared `backlinks` table. The UNIQUE constraint
(source_url, page_id, data_source) keeps each (link → page) pair from a
given source idempotent — re-runs do not create duplicates. When the
SAME link is discovered by BOTH sources, two rows exist (one per
data_source) so the operator can see which feed found it; the report
de-duplicates by `source_url` for the count.

Scope selection
---------------
- `--all-pillars`     (default) — pulls backlinks for every page where
                                  page_type IN ('pillar', 'service', 'home').
- `--page-id N`       — restrict to one DB page.
- `--url URL`         — restrict to one URL (must already exist in `pages`).
- `--domain DOMAIN`   — pull domain-level backlinks via DataForSEO and
                        attach them to a synthetic "root" pages row if
                        one exists for that domain's home; otherwise skip GSC.

DataForSEO degradation
----------------------
The Backlinks API requires its own subscription. When unavailable the
module reports the access-denied error inline and falls back to
GSC-only mode rather than crashing.

GSC degradation
---------------
Search Console exposes external links via Search Analytics with
dimensions=[page] — that gives us the destination + impressions but no
source URL. For genuine backlink discovery we'd need the "Links" report,
which isn't in the public API. So this module's GSC path approximates
backlink confirmation: when DataForSEO returns a backlink, we mark it
also-confirmed-by-GSC if the target page appears in GSC analytics.

Outputs
-------
- `backlinks` rows upserted (idempotent)
- `outputs/audits/backlinks_<date>.md` — narrative (top growth pages,
   new this run, vanished domains, etc.)

Usage
-----
    # Monthly cadence — every pillar + service page
    python -m offpage_links.backlink_tracker

    # One page only
    python -m offpage_links.backlink_tracker --page-id 42

    # Domain-level pull
    python -m offpage_links.backlink_tracker --domain damcogroup.com

    # Skip Anthropic-credit-free GSC pass (DataForSEO only)
    python -m offpage_links.backlink_tracker --skip-gsc

    # Dry run — fetch + write report; skip DB upserts
    python -m offpage_links.backlink_tracker --dry-run
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from common.connectors.dataforseo import (
    DataForSEOAccessDenied,
    DataForSEOError,
    get_backlinks,
)
from common.database import connection, fetch_all, fetch_one, record_agent_run


logger = logging.getLogger("backlink_tracker")
AGENT_NAME = "offpage_links.backlink_tracker"

OUTPUT_DIR = Path(__file__).resolve().parent.parent / "outputs" / "audits"

DEFAULT_PER_TARGET_LIMIT = 1000


# ---------------------------------------------------------------------------
# Read phase
# ---------------------------------------------------------------------------

def load_target_pages(page_id: int | None, url: str | None,
                       all_pillars: bool) -> list[dict]:
    """Return rows of (id, url, page_type) for pages this run should sweep."""
    if page_id is not None:
        return fetch_all("SELECT id, url, page_type FROM pages WHERE id = %s", [page_id])
    if url is not None:
        return fetch_all("SELECT id, url, page_type FROM pages WHERE url = %s", [url])
    if all_pillars:
        return fetch_all(
            "SELECT id, url, page_type FROM pages "
            "WHERE page_type IN ('pillar', 'service', 'home') "
            "ORDER BY page_type, url"
        )
    return []


def load_existing_backlinks(page_id: int) -> set[tuple[str, str]]:
    """Return (source_url, data_source) tuples already on file for this page."""
    rows = fetch_all(
        "SELECT source_url, data_source FROM backlinks WHERE page_id = %s",
        [page_id],
    )
    return {(r["source_url"], r["data_source"]) for r in rows}


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------

def pull_dataforseo(target: str, limit: int) -> tuple[list[dict] | None, str | None]:
    """Returns (rows, None) on success; (None, error_message) on failure."""
    try:
        rows = get_backlinks(target, limit=limit, mode="as_is")
        return rows, None
    except DataForSEOAccessDenied as exc:
        return None, f"DataForSEO Backlinks: subscription required ({exc})"
    except DataForSEOError as exc:
        return None, f"DataForSEO Backlinks error: {exc}"


def pull_gsc_pages_seen(days: int = 30) -> set[str]:
    """
    GSC's public API doesn't expose external link sources, but it DOES
    expose which Damco pages received clicks/impressions from external
    queries. If a DataForSEO-discovered backlink points to a page that
    GSC also reports activity for in the last N days, we treat that
    as cross-source confirmation.

    Returns: the set of Damco page URLs that appear in GSC over the
    last `days`.
    """
    try:
        from common.connectors.gsc import get_search_analytics
        rows = get_search_analytics(
            start_date=(date.today() - timedelta(days=days)).isoformat(),
            end_date=date.today().isoformat(),
            dimensions=("page",),
            row_limit=25000,
        )
        return {(r["keys"][0] if r.get("keys") else "") for r in rows}
    except Exception as exc:
        logger.warning("GSC fetch failed (%s) — continuing without GSC confirmation.", exc)
        return set()


# ---------------------------------------------------------------------------
# Write phase
# ---------------------------------------------------------------------------

def classify_link_type(dofollow: bool | None, raw: dict) -> str:
    """Map DataForSEO booleans + attrs into our enum."""
    if dofollow is None:
        return "unknown"
    if not dofollow:
        # Could be nofollow, ugc, or sponsored — DataForSEO doesn't always disambiguate
        rel = (raw.get("rel") or "").lower()
        if "ugc" in rel:
            return "ugc"
        if "sponsored" in rel:
            return "sponsored"
        return "nofollow"
    return "dofollow"


def normalize_domain(url: str | None) -> str:
    if not url:
        return ""
    host = urlparse(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def upsert_backlinks(page_id: int, rows: list[dict], data_source: str) -> int:
    """
    Idempotent insert. Returns the number of NEW rows inserted (not
    updates). DataForSEO returns "rank" as 0-1000 — store as int.
    """
    if not rows:
        return 0
    sql = """
        INSERT INTO backlinks
            (page_id, source_url, source_domain, domain_authority,
             link_type, anchor_text, date_discovered, data_source)
        VALUES (%s, %s, %s, %s, %s, %s, CURRENT_DATE, %s)
        ON CONFLICT (source_url, page_id, data_source)
        DO UPDATE SET
            domain_authority = EXCLUDED.domain_authority,
            link_type        = EXCLUDED.link_type,
            anchor_text      = EXCLUDED.anchor_text
        RETURNING (xmax = 0) AS inserted
    """
    inserted = 0
    with connection() as conn:
        with conn.cursor() as cur:
            for r in rows:
                source_url = r.get("source_url") or ""
                if not source_url:
                    continue
                cur.execute(
                    sql,
                    (
                        page_id,
                        source_url,
                        r.get("source_domain") or normalize_domain(source_url),
                        int(r["rank"]) if r.get("rank") is not None else None,
                        classify_link_type(r.get("dofollow"), r.get("raw") or {}),
                        (r.get("anchor") or "")[:500] or None,
                        data_source,
                    ),
                )
                if cur.fetchone()[0]:
                    inserted += 1
    return inserted


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def build_per_page_summary(page: dict, dfs_rows: list[dict] | None,
                            dfs_error: str | None, new_count: int,
                            existing_count: int, gsc_confirmed: bool) -> dict:
    distinct_domains: set[str] = set()
    dofollow = nofollow = 0
    da_scores: list[int] = []
    if dfs_rows:
        for r in dfs_rows:
            d = (r.get("source_domain") or "").lower()
            if d:
                distinct_domains.add(d)
            if r.get("dofollow") is True:
                dofollow += 1
            elif r.get("dofollow") is False:
                nofollow += 1
            if r.get("rank") is not None:
                da_scores.append(int(r["rank"]))

    return {
        "page_id":          page["id"],
        "url":              page["url"],
        "page_type":        page["page_type"],
        "dataforseo_rows":  len(dfs_rows or []),
        "dataforseo_err":   dfs_error,
        "new_in_run":       new_count,
        "existing":         existing_count,
        "distinct_domains": len(distinct_domains),
        "dofollow":         dofollow,
        "nofollow":         nofollow,
        "avg_da":           round(sum(da_scores) / len(da_scores), 1) if da_scores else None,
        "gsc_confirmed":    gsc_confirmed,
    }


def write_markdown(per_page: list[dict], dfs_blocked: bool,
                   gsc_pages: int) -> Path:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUTPUT_DIR / f"backlinks_{date.today().isoformat()}.md"

    p: list[str] = []
    p.append(f"# Backlink Inventory Refresh — {date.today().isoformat()}")
    p.append("")
    p.append(f"_Generated by `{AGENT_NAME}`._")
    p.append("")

    p.append("## Summary")
    p.append("")
    total_rows = sum(x["dataforseo_rows"] for x in per_page)
    total_new = sum(x["new_in_run"] for x in per_page)
    total_domains = len({d for x in per_page for d in [] })  # placeholder, recompute below
    p.append("| Metric | Value |")
    p.append("|---|---:|")
    p.append(f"| Pages swept | {len(per_page)} |")
    p.append(f"| DataForSEO rows fetched | {total_rows} |")
    p.append(f"| Backlinks new this run | {total_new} |")
    p.append(f"| GSC pages observed (last 30d) | {gsc_pages} |")
    if dfs_blocked:
        p.append(f"| DataForSEO Backlinks API | **blocked — subscription required** |")
    p.append("")

    if not per_page:
        p.append("_No target pages selected. Use `--all-pillars`, `--page-id`, `--url`, or `--domain`._")
        path.write_text("\n".join(p), encoding="utf-8")
        return path

    # Per-page table
    p.append("## Per-page results")
    p.append("")
    p.append("| Page | Type | DFS rows | New | Existing | Distinct domains | Dofollow | Avg DA | GSC ✓ |")
    p.append("|---|---|---:|---:|---:|---:|---:|---:|:-:|")
    for x in sorted(per_page, key=lambda r: -r["new_in_run"]):
        gsc = "✓" if x["gsc_confirmed"] else "—"
        avg_da = f"{x['avg_da']}" if x["avg_da"] is not None else "—"
        p.append(f"| `{x['url'][:60]}` | {x['page_type'] or '—'} | {x['dataforseo_rows']} | "
                 f"{x['new_in_run']} | {x['existing']} | {x['distinct_domains']} | "
                 f"{x['dofollow']} | {avg_da} | {gsc} |")
    p.append("")

    # Errors
    errs = [x for x in per_page if x["dataforseo_err"]]
    if errs:
        p.append("## Errors")
        p.append("")
        for e in errs:
            p.append(f"- `{e['url']}`: {e['dataforseo_err']}")
        p.append("")

    path.write_text("\n".join(p), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(page_id: int | None = None,
        url: str | None = None,
        domain: str | None = None,
        all_pillars: bool = True,
        per_target_limit: int = DEFAULT_PER_TARGET_LIMIT,
        skip_gsc: bool = False,
        dry_run: bool = False) -> dict:
    start = time.monotonic()
    pages = load_target_pages(page_id, url, all_pillars and not domain)

    # Domain mode: treat the domain root as the target — but only if the
    # `pages` table has a row for the home URL, since `backlinks.page_id`
    # is a FK.
    if domain:
        host = domain.lstrip("https://").lstrip("http://").rstrip("/")
        home_candidates = [f"https://{host}/", f"https://www.{host}/", f"http://{host}/"]
        for cand in home_candidates:
            row = fetch_one("SELECT id, url, page_type FROM pages WHERE url = %s", [cand])
            if row:
                pages = [row]
                break

    if not pages:
        logger.warning("No target pages resolved. Try --all-pillars or --url.")
        return {"status": "skipped", "reason": "no targets"}

    logger.info("Targeting %d page(s) for backlink refresh", len(pages))

    # GSC page presence (once, shared across all targets)
    gsc_pages: set[str] = set() if skip_gsc else pull_gsc_pages_seen(days=30)

    per_page_summaries: list[dict] = []
    dfs_blocked = False
    total_new = 0
    errors: list[str] = []

    for page in pages:
        logger.info("[page_id=%s] %s", page["id"], page["url"])
        existing = load_existing_backlinks(page["id"])
        rows, err = pull_dataforseo(page["url"], per_target_limit)
        if err and "subscription required" in err:
            dfs_blocked = True
        if err:
            errors.append(f"{page['url']}: {err}")

        new_count = 0
        if rows and not dry_run:
            new_count = upsert_backlinks(page["id"], rows, "dataforseo")
            total_new += new_count

        gsc_confirmed = page["url"] in gsc_pages
        per_page_summaries.append(
            build_per_page_summary(page, rows, err, new_count, len(existing), gsc_confirmed)
        )

    md_path = write_markdown(per_page_summaries, dfs_blocked, len(gsc_pages))

    duration = time.monotonic() - start
    status = "partial" if errors else "success"

    if not dry_run:
        record_agent_run(
            agent_name=AGENT_NAME,
            status=status,
            records_processed=total_new,
            errors=errors[:25],
            duration_seconds=round(duration, 2),
            metadata={
                "run_date":          date.today().isoformat(),
                "pages_swept":       len(pages),
                "new_backlinks":     total_new,
                "dataforseo_blocked": dfs_blocked,
                "gsc_pages_observed": len(gsc_pages),
                "skip_gsc":          skip_gsc,
                "md_path":           str(md_path),
            },
        )

    # Console summary
    print()
    print(f"  {'=' * 72}")
    print(f"   BACKLINK TRACKER -- {date.today().isoformat()}")
    print(f"  {'=' * 72}")
    print()
    print(f"  Pages swept:           {len(pages)}")
    print(f"  New backlinks:         {total_new}")
    print(f"  GSC pages (30d):       {len(gsc_pages)}")
    if dfs_blocked:
        print(f"  DataForSEO Backlinks:  BLOCKED — subscription required")
    print(f"  Errors:                {len(errors)}")
    print(f"  Report:                {md_path}")
    print(f"  Duration:              {duration:.2f}s")
    print()

    return {
        "status":           status,
        "pages_swept":      len(pages),
        "new_backlinks":    total_new,
        "dataforseo_blocked": dfs_blocked,
        "md_path":          str(md_path),
        "duration_seconds": round(duration, 2),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Damco Backlink Tracker (DataForSEO + GSC)")
    target = parser.add_mutually_exclusive_group()
    target.add_argument("--page-id", type=int, help="Restrict to a single pages.id")
    target.add_argument("--url", help="Restrict to a single page URL")
    target.add_argument("--domain", help="Treat domain root as target (requires home page in `pages`)")
    parser.add_argument("--limit", type=int, default=DEFAULT_PER_TARGET_LIMIT,
                        help=f"Backlinks per target (default: {DEFAULT_PER_TARGET_LIMIT})")
    parser.add_argument("--skip-gsc", action="store_true",
                        help="Skip GSC confirmation pass (avoids OAuth prompt)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Fetch + report, but skip DB writes")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s  %(name)s  %(levelname)s  %(message)s",
    )

    run(
        page_id=args.page_id,
        url=args.url,
        domain=args.domain,
        all_pillars=not (args.page_id or args.url or args.domain),
        per_target_limit=args.limit,
        skip_gsc=args.skip_gsc,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    main()
