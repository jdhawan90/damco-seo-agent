"""
GSC Enrichment — 14-day average position + click/impression data
================================================================

Queries Google Search Console for all queries that generated impressions
on the tracked site over the last 14 days, then matches them against
keywords in the database and stores the GSC metrics alongside DataForSEO
SERP rankings.

This gives a dual-lens view:
  - DataForSEO: where Damco appears right now (point-in-time SERP snapshot)
  - GSC:        how Google measures performance over 14 days (avg position,
                actual clicks, impressions, CTR — smoothed, real-user data)

Usage
-----
    # Standalone
    python -m keyword_intelligence.gsc_enrichment

    # As part of rank tracker (called automatically when GSC is configured)
    python -m keyword_intelligence.rank_tracker  # includes GSC step

    # Custom lookback window
    python -m keyword_intelligence.gsc_enrichment --days 30

GSC data lag
------------
Google Search Console data has a ~3 day lag. When --days 14 is specified,
the actual query window is (today - 17 days) to (today - 3 days) to ensure
14 full days of data.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from common.config import settings
from common.database import connection, fetch_all, record_agent_run


logger = logging.getLogger("gsc_enrichment")

AGENT_NAME = "keyword_intelligence.gsc_enrichment"

# GSC data lag — latest available data is typically 3 days old
GSC_DATA_LAG_DAYS = 3


def rank_bucket(position: float | None) -> str:
    if position is None:
        return "not-found"
    pos = round(position)
    if pos <= 5:
        return "1-5"
    if pos <= 10:
        return "5-10"
    if pos <= 20:
        return "10-20"
    if pos <= 50:
        return "20-50"
    return "50+"


def fetch_gsc_data(lookback_days: int = 14) -> list[dict]:
    """
    Pull aggregated search analytics from GSC for the lookback period.

    Returns a list of dicts with keys:
        query, clicks, impressions, ctr, position (avg)

    The query window accounts for GSC's ~3 day data lag.
    """
    from common.connectors.gsc import get_search_analytics

    end_date = date.today() - timedelta(days=GSC_DATA_LAG_DAYS)
    start_date = end_date - timedelta(days=lookback_days)

    logger.info("Querying GSC: %s to %s (%d days)", start_date, end_date, lookback_days)

    rows = get_search_analytics(
        start_date=start_date.isoformat(),
        end_date=end_date.isoformat(),
        dimensions=["query"],
        row_limit=25000,
    )

    results = []
    for row in rows:
        keys = row.get("keys", [])
        query = keys[0] if keys else ""
        results.append({
            "query": query.lower().strip(),
            "clicks": row.get("clicks", 0),
            "impressions": row.get("impressions", 0),
            "ctr": round(row.get("ctr", 0), 4),
            "position": round(row.get("position", 0), 1),
        })

    logger.info("GSC returned %d queries", len(results))
    return results


def match_and_store(gsc_data: list[dict], lookback_days: int, dry_run: bool = False) -> dict:
    """
    Match GSC query data against tracked keywords and store matches.

    Matching strategy:
      1. Exact match (GSC query == keyword, case-insensitive)
      2. Contained match (tracked keyword appears within GSC query)
         — only if no exact match found for that keyword

    Returns summary stats.
    """
    # Load active keywords from DB
    keywords = fetch_all("SELECT id, keyword FROM keywords WHERE status = 'active'")
    kw_map = {kw["keyword"].lower().strip(): kw["id"] for kw in keywords}

    # Build lookup from GSC data
    gsc_by_query: dict[str, dict] = {r["query"]: r for r in gsc_data}

    run_date = date.today()
    matched = 0
    not_matched = 0
    stored = 0

    matches: list[dict] = []

    for kw_text, kw_id in kw_map.items():
        gsc_row = gsc_by_query.get(kw_text)

        if not gsc_row:
            # Try contained match — find GSC queries that contain our keyword
            # Pick the one with highest impressions if multiple match
            candidates = [
                r for r in gsc_data
                if kw_text in r["query"] or r["query"] in kw_text
            ]
            if candidates:
                gsc_row = max(candidates, key=lambda r: r["impressions"])
                logger.debug("Fuzzy match: '%s' → GSC query '%s' (%d impressions)",
                             kw_text, gsc_row["query"], gsc_row["impressions"])

        if gsc_row:
            matched += 1
            matches.append({
                "keyword_id": kw_id,
                "keyword": kw_text,
                "gsc_query": gsc_row["query"],
                "position": gsc_row["position"],
                "clicks": gsc_row["clicks"],
                "impressions": gsc_row["impressions"],
                "ctr": gsc_row["ctr"],
            })
        else:
            not_matched += 1
            logger.debug("No GSC match for: '%s'", kw_text)

    # Store matches
    if not dry_run and matches:
        with connection() as conn:
            with conn.cursor() as cur:
                for m in matches:
                    try:
                        cur.execute("""
                            INSERT INTO keyword_rankings
                                (keyword_id, date, rank_position, rank_bucket,
                                 clicks, impressions, ctr, source)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, 'gsc')
                            ON CONFLICT (keyword_id, date, source)
                            DO UPDATE SET
                                rank_position = EXCLUDED.rank_position,
                                rank_bucket   = EXCLUDED.rank_bucket,
                                clicks        = EXCLUDED.clicks,
                                impressions   = EXCLUDED.impressions,
                                ctr           = EXCLUDED.ctr
                        """, (
                            m["keyword_id"],
                            run_date,
                            round(m["position"]),
                            rank_bucket(m["position"]),
                            m["clicks"],
                            m["impressions"],
                            m["ctr"],
                        ))
                        stored += 1
                    except Exception as exc:
                        logger.error("Failed to store GSC data for keyword_id=%s: %s",
                                     m["keyword_id"], exc)

    return {
        "total_gsc_queries": len(gsc_data),
        "tracked_keywords": len(kw_map),
        "matched": matched,
        "not_matched": not_matched,
        "stored": stored,
        "matches": matches,
    }


def print_summary(stats: dict, lookback_days: int) -> None:
    """Print a console summary of the GSC enrichment run."""
    print()
    print(f"  {'=' * 60}")
    print(f"   GSC Enrichment — {lookback_days}-day average")
    print(f"  {'=' * 60}")
    print()
    print(f"  GSC queries returned:    {stats['total_gsc_queries']}")
    print(f"  Tracked keywords:        {stats['tracked_keywords']}")
    print(f"  Matched to GSC data:     {stats['matched']}")
    print(f"  No GSC data:             {stats['not_matched']}")
    print(f"  Stored in DB:            {stats['stored']}")
    print()

    if stats["matches"]:
        print(f"  {'KEYWORD':<40} {'GSC Pos':>8} {'Clicks':>8} {'Impr':>8} {'CTR':>8}")
        print(f"  {'-' * 72}")
        for m in sorted(stats["matches"], key=lambda x: x["position"]):
            print(f"  {m['keyword'][:38]:<40} {m['position']:>8.1f} {m['clicks']:>8} "
                  f"{m['impressions']:>8} {m['ctr']:>7.2%}")
        print()


def run(lookback_days: int = 14, dry_run: bool = False) -> dict:
    """Execute a full GSC enrichment run."""
    start_time = time.monotonic()

    # Check if GSC is configured
    if not settings.GSC_SITE_URL:
        logger.warning("GSC_SITE_URL not configured — skipping GSC enrichment")
        return {"status": "skipped", "reason": "GSC not configured"}

    try:
        gsc_data = fetch_gsc_data(lookback_days)
    except Exception as exc:
        logger.error("GSC fetch failed: %s", exc)
        if not dry_run:
            record_agent_run(
                agent_name=AGENT_NAME,
                status="error",
                errors=[str(exc)],
                duration_seconds=round(time.monotonic() - start_time, 2),
            )
        return {"status": "error", "error": str(exc)}

    stats = match_and_store(gsc_data, lookback_days, dry_run=dry_run)
    duration = time.monotonic() - start_time

    if not dry_run:
        record_agent_run(
            agent_name=AGENT_NAME,
            status="success",
            records_processed=stats["stored"],
            duration_seconds=round(duration, 2),
            metadata={
                "lookback_days": lookback_days,
                "gsc_queries": stats["total_gsc_queries"],
                "matched": stats["matched"],
                "not_matched": stats["not_matched"],
            },
        )

    print_summary(stats, lookback_days)
    return {"status": "success", **stats}


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Enrich keyword rankings with GSC average position + click data"
    )
    parser.add_argument("--days", type=int, default=14,
                        help="Lookback window in days (default: 14)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Fetch GSC data but don't write to DB")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s  %(name)s  %(levelname)s  %(message)s",
    )

    run(lookback_days=args.days, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
