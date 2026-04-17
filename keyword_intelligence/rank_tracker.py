"""
Keyword Rank Tracker Agent — Phase 1
=====================================

Standard agent lifecycle:
  Read    — fetch active keywords from DB, call DataForSEO SERP API
  Process — find Damco positions, compute buckets, detect movement
  Write   — upsert into keyword_rankings, log to agent_runs
  Notify  — (future) Slack/email alerts for drops and striking-distance keywords

Usage
-----
    # Track all active keywords (standard queue, cheapest)
    python -m keyword_intelligence.rank_tracker

    # Track only a specific offering
    python -m keyword_intelligence.rank_tracker --offering "AI Development"

    # Use live queue for immediate results (3x more expensive)
    python -m keyword_intelligence.rank_tracker --queue live

    # Dry run — call the API but don't write to DB
    python -m keyword_intelligence.rank_tracker --dry-run
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import date, datetime
from pathlib import Path
from typing import Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from common.config import settings
from common.database import connection, fetch_all, record_agent_run
from common.connectors.dataforseo import get_serp_rankings, DataForSEOError


logger = logging.getLogger("rank_tracker")

AGENT_NAME = "keyword_intelligence.rank_tracker"

# Damco brand domains to match in SERP results (case-insensitive)
BRAND_DOMAINS = {"damcogroup.com", "achieva.ai", "damcodigital.com"}

# DataForSEO batch limit
BATCH_SIZE = 100


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def rank_bucket(position: int | None) -> str:
    if position is None:
        return "not-found"
    if position <= 5:
        return "1-5"
    if position <= 10:
        return "5-10"
    if position <= 20:
        return "10-20"
    if position <= 50:
        return "20-50"
    return "50+"


def find_brand_position(serp_items: list[dict]) -> dict | None:
    """
    Find the first Damco domain in a list of organic SERP items.
    Returns the item dict with rank_group, domain, url, title — or None.
    """
    for item in serp_items:
        domain = (item.get("domain") or "").lower()
        for brand in BRAND_DOMAINS:
            if brand in domain:
                return item
    return None


# ---------------------------------------------------------------------------
# Read phase
# ---------------------------------------------------------------------------

def load_keywords(offering: str | None = None) -> list[dict]:
    """Fetch active keywords from the database."""
    sql = "SELECT id, keyword, offering, target_url FROM keywords WHERE status = 'active'"
    params: list = []
    if offering:
        sql += " AND offering = %s"
        params.append(offering)
    sql += " ORDER BY offering, keyword"
    return fetch_all(sql, params)


# ---------------------------------------------------------------------------
# Process phase
# ---------------------------------------------------------------------------

def fetch_rankings(keywords: list[dict], queue: str) -> list[dict]:
    """
    Call DataForSEO for all keywords in batches.
    Returns a list of result dicts enriched with keyword_id and brand position.
    """
    results: list[dict] = []
    keyword_texts = [kw["keyword"] for kw in keywords]
    kw_id_map = {kw["keyword"]: kw["id"] for kw in keywords}

    # Batch into groups of BATCH_SIZE
    for i in range(0, len(keyword_texts), BATCH_SIZE):
        batch = keyword_texts[i : i + BATCH_SIZE]
        batch_num = (i // BATCH_SIZE) + 1
        total_batches = (len(keyword_texts) + BATCH_SIZE - 1) // BATCH_SIZE
        logger.info("Batch %d/%d: %d keywords", batch_num, total_batches, len(batch))

        try:
            serp_results = get_serp_rankings(batch, queue=queue)
        except DataForSEOError as exc:
            logger.error("DataForSEO batch %d failed: %s", batch_num, exc)
            # Record individual failures
            for kw_text in batch:
                results.append({
                    "keyword_id": kw_id_map.get(kw_text),
                    "keyword": kw_text,
                    "rank_position": None,
                    "rank_bucket": "not-found",
                    "url_found": None,
                    "domain_found": None,
                    "error": str(exc),
                })
            continue

        # Match SERP results back to keyword IDs
        for serp in serp_results:
            kw_text = serp["keyword"]
            brand_hit = find_brand_position(serp.get("items") or [])
            results.append({
                "keyword_id": kw_id_map.get(kw_text),
                "keyword": kw_text,
                "rank_position": brand_hit["rank_group"] if brand_hit else None,
                "rank_bucket": rank_bucket(brand_hit["rank_group"] if brand_hit else None),
                "url_found": brand_hit.get("url") if brand_hit else None,
                "domain_found": brand_hit.get("domain") if brand_hit else None,
                "error": None,
            })

    return results


# ---------------------------------------------------------------------------
# Write phase
# ---------------------------------------------------------------------------

def store_rankings(results: list[dict], run_date: date) -> tuple[int, int]:
    """
    Upsert ranking results into keyword_rankings.
    Returns (inserted_count, error_count).
    """
    inserted = 0
    errors = 0

    with connection() as conn:
        with conn.cursor() as cur:
            for r in results:
                if r.get("error"):
                    errors += 1
                    continue
                try:
                    cur.execute("""
                        INSERT INTO keyword_rankings
                            (keyword_id, date, rank_position, rank_bucket, url_found, source)
                        VALUES (%s, %s, %s, %s, %s, 'dataforseo')
                        ON CONFLICT (keyword_id, date, source)
                        DO UPDATE SET
                            rank_position = EXCLUDED.rank_position,
                            rank_bucket   = EXCLUDED.rank_bucket,
                            url_found     = EXCLUDED.url_found
                    """, (
                        r["keyword_id"],
                        run_date,
                        r["rank_position"],
                        r["rank_bucket"],
                        r["url_found"],
                    ))
                    inserted += 1
                except Exception as exc:
                    logger.error("Failed to store ranking for keyword_id=%s: %s",
                                 r["keyword_id"], exc)
                    errors += 1

    return inserted, errors


# ---------------------------------------------------------------------------
# Report (console summary)
# ---------------------------------------------------------------------------

def print_summary(results: list[dict], run_date: date) -> None:
    """Print a console summary table of the run results."""
    print()
    print(f"  {'=' * 72}")
    print(f"   DAMCO Rank Tracker — {run_date.isoformat()}")
    print(f"  {'=' * 72}")
    print()

    # Bucket distribution
    buckets: dict[str, int] = {}
    for r in results:
        b = r["rank_bucket"]
        buckets[b] = buckets.get(b, 0) + 1

    print("  Bucket Distribution:")
    for bucket_name in ["1-5", "5-10", "10-20", "20-50", "50+", "not-found"]:
        count = buckets.get(bucket_name, 0)
        bar = "#" * count
        print(f"    {bucket_name:>10}  {count:>3}  {bar}")
    print()

    # Detailed table
    print(f"  {'KEYWORD':<45} {'POS':>5}  {'BUCKET':>10}  {'DOMAIN'}")
    print(f"  {'-' * 80}")
    for r in sorted(results, key=lambda x: (x["rank_position"] or 999)):
        kw = r["keyword"][:43]
        pos = str(r["rank_position"]) if r["rank_position"] else "N/A"
        bucket = r["rank_bucket"]
        domain = r.get("domain_found") or "-"
        if r.get("error"):
            domain = f"ERROR: {r['error'][:30]}"
        print(f"  {kw:<45} {pos:>5}  {bucket:>10}  {domain}")
    print()

    # Striking distance
    striking = [r for r in results if r["rank_position"] and 11 <= r["rank_position"] <= 20]
    if striking:
        print(f"  STRIKING DISTANCE (positions 11-20): {len(striking)} keywords")
        for r in sorted(striking, key=lambda x: x["rank_position"]):
            print(f"    pos {r['rank_position']:>3}  {r['keyword']}")
        print()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(offering: str | None = None, queue: str = "standard", dry_run: bool = False,
        skip_gsc: bool = False, gsc_days: int = 14) -> dict:
    """
    Execute a full rank tracking run. Returns a summary dict.
    """
    start_time = time.monotonic()
    run_date = date.today()

    # Read
    keywords = load_keywords(offering)
    if not keywords:
        logger.warning("No active keywords found%s", f" for offering={offering}" if offering else "")
        return {"status": "skipped", "reason": "no keywords"}

    logger.info("Tracking %d keywords (queue=%s, date=%s)", len(keywords), queue, run_date)

    # Process
    results = fetch_rankings(keywords, queue)

    # Summary stats
    total = len(results)
    found = sum(1 for r in results if r["rank_position"] is not None)
    errors = sum(1 for r in results if r.get("error"))
    duration = time.monotonic() - start_time

    # Write
    if dry_run:
        logger.info("DRY RUN — skipping database write")
        inserted, write_errors = 0, 0
    else:
        inserted, write_errors = store_rankings(results, run_date)
        record_agent_run(
            agent_name=AGENT_NAME,
            status="success" if errors == 0 else "partial",
            records_processed=inserted,
            errors=[r["error"] for r in results if r.get("error")],
            duration_seconds=round(duration, 2),
            metadata={
                "run_date": run_date.isoformat(),
                "queue": queue,
                "offering_filter": offering,
                "total_keywords": total,
                "found": found,
                "not_found": total - found - errors,
                "errors": errors,
            },
        )

    # Notify (console for now)
    print_summary(results, run_date)
    print(f"  Keywords tracked: {total}")
    print(f"  Brand found:      {found}")
    print(f"  Not found:        {total - found - errors}")
    print(f"  API errors:       {errors}")
    print(f"  DB writes:        {inserted}")
    print(f"  Duration:         {duration:.1f}s")
    print(f"  Estimated cost:   ~${total * 0.0006:.4f} (standard queue)")
    print()

    # GSC Enrichment — pull 14-day average position + clicks/impressions
    gsc_stats: dict | None = None
    if not dry_run and not skip_gsc:
        try:
            from keyword_intelligence.gsc_enrichment import run as gsc_run
            gsc_stats = gsc_run(lookback_days=gsc_days, dry_run=dry_run)
        except Exception as exc:
            logger.warning("GSC enrichment failed (non-fatal): %s", exc)
            gsc_stats = {"status": "error", "error": str(exc)}

    return {
        "status": "success" if errors == 0 else "partial",
        "run_date": run_date.isoformat(),
        "total": total,
        "found": found,
        "errors": errors,
        "inserted": inserted,
        "duration_seconds": round(duration, 2),
        "results": results,
        "gsc": gsc_stats,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Damco Keyword Rank Tracker — Phase 1 Agent"
    )
    parser.add_argument("--offering", help="Track only keywords for a specific offering")
    parser.add_argument("--queue", default="standard", choices=["standard", "live"],
                        help="DataForSEO queue tier (default: standard)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Fetch rankings but don't write to DB")
    parser.add_argument("--skip-gsc", action="store_true",
                        help="Skip GSC enrichment step")
    parser.add_argument("--gsc-days", type=int, default=14,
                        help="GSC lookback window in days (default: 14)")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Enable debug logging")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s  %(name)s  %(levelname)s  %(message)s",
    )

    run(offering=args.offering, queue=args.queue, dry_run=args.dry_run,
        skip_gsc=args.skip_gsc, gsc_days=args.gsc_days)


if __name__ == "__main__":
    main()
