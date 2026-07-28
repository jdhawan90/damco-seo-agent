"""
GA4 Sync — pulls behaviour metrics into the shared database.
============================================================

Standard agent lifecycle:
  Read    — GA4 Data API via common.connectors.ga4 (never called directly)
  Process — resolve each landing page to a `pages` row where one exists
  Write   — upsert ga4_landing_pages + ga4_channel_totals
  Notify  — console summary; log to agent_runs

Why an agent and not a live dashboard call
------------------------------------------
The dashboard reads Postgres only. Pulling GA4 at page load would make tiles
slow, inconsistent between viewers, and blank whenever Google is having a bad
minute. This runs on a schedule and the dashboard reads what it wrote.

Landing-page resolution
-----------------------
GA4 reports a path (`/ai-development-services/`), sometimes with a query
string. `pages.url` holds absolute URLs across three domains. Matching is
therefore path-based, and **ambiguous or unmatched paths are left NULL rather
than guessed** — a wrong join would credit one offering's conversions to
another, which is worse than an unattributed row.

Usage
-----
    # Default: last 28 days, organic search only
    python -m keyword_intelligence.ga4_sync

    # Longer window
    python -m keyword_intelligence.ga4_sync --days 90

    # Every channel, not just organic
    python -m keyword_intelligence.ga4_sync --all-channels

    # See what conversion events actually fire before trusting a conversion number
    python -m keyword_intelligence.ga4_sync --events

    # Fetch and report without writing
    python -m keyword_intelligence.ga4_sync --dry-run
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from common.connectors import ga4
from common.database import connection, fetch_all, record_agent_run
from common.tenant import profile


logger = logging.getLogger("ga4_sync")
AGENT_NAME = "keyword_intelligence.ga4_sync"


def _norm_path(value: str) -> str:
    """
    Normalize a landing page to a comparable path.

    GA4 emits paths with and without trailing slashes, sometimes with query
    strings, occasionally as '(not set)'. `pages.url` holds absolute URLs. Both
    sides collapse to a lowercase path with no trailing slash so the join is
    stable.
    """
    if not value or value.startswith("("):
        return ""
    v = value.strip()
    if "://" in v:
        v = urlparse(v).path or "/"
    v = v.split("?", 1)[0].split("#", 1)[0].lower()
    if len(v) > 1:
        v = v.rstrip("/")
    return v or "/"


def build_page_index() -> tuple[dict[str, int], set[str]]:
    """
    Path -> page_id for this tenant's own pages.

    Returns the index plus the set of paths that appear on more than one owned
    domain. Those are dropped from the index: /contact/ existing on two
    properties makes the mapping genuinely ambiguous, and picking one silently
    would attribute traffic to the wrong site.
    """
    p = profile()
    index: dict[str, int] = {}
    seen: dict[str, set[str]] = {}

    for row in fetch_all("SELECT id, url FROM pages WHERE url IS NOT NULL"):
        url = row["url"]
        if not p.owns(url):
            continue
        path = _norm_path(url)
        if not path:
            continue
        host = urlparse(url).netloc.lower()
        seen.setdefault(path, set()).add(host)
        index.setdefault(path, row["id"])

    ambiguous = {path for path, hosts in seen.items() if len(hosts) > 1}
    for path in ambiguous:
        index.pop(path, None)
    if ambiguous:
        logger.info("%d path(s) exist on more than one owned domain and are left "
                    "unattributed rather than guessed", len(ambiguous))
    return index, ambiguous


def upsert_landing_pages(rows: list[dict], window_end: date, window_days: int,
                         index: dict[str, int]) -> dict:
    """Upsert landing-page metrics. Returns counters."""
    stats = {"written": 0, "matched": 0, "unmatched": 0}
    if not rows:
        return stats

    sql = """
        INSERT INTO ga4_landing_pages
            (window_end, window_days, landing_page, channel, page_id,
             sessions, engaged_sessions, engagement_rate, conversions,
             revenue, avg_duration_sec)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (window_end, window_days, landing_page, channel) DO UPDATE SET
            page_id          = EXCLUDED.page_id,
            sessions         = EXCLUDED.sessions,
            engaged_sessions = EXCLUDED.engaged_sessions,
            engagement_rate  = EXCLUDED.engagement_rate,
            conversions      = EXCLUDED.conversions,
            revenue          = EXCLUDED.revenue,
            avg_duration_sec = EXCLUDED.avg_duration_sec
    """
    with connection() as conn:
        with conn.cursor() as cur:
            for r in rows:
                lp = r.get("landingPage") or ""
                page_id = index.get(_norm_path(lp))
                if page_id:
                    stats["matched"] += 1
                else:
                    stats["unmatched"] += 1
                cur.execute(sql, (
                    window_end, window_days, lp[:2000],
                    r.get("sessionDefaultChannelGroup") or "Organic Search",
                    page_id,
                    int(r.get("sessions") or 0),
                    int(r.get("engagedSessions") or 0),
                    round(float(r.get("engagementRate") or 0), 4),
                    round(float(r.get("conversions") or 0), 2),
                    round(float(r.get("totalRevenue") or 0), 2),
                    round(float(r.get("averageSessionDuration") or 0), 2),
                ))
                stats["written"] += 1
        conn.commit()
    return stats


def upsert_channels(rows: list[dict], window_end: date, window_days: int) -> int:
    if not rows:
        return 0
    sql = """
        INSERT INTO ga4_channel_totals
            (window_end, window_days, channel, sessions, engaged_sessions,
             conversions, revenue)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (window_end, window_days, channel) DO UPDATE SET
            sessions         = EXCLUDED.sessions,
            engaged_sessions = EXCLUDED.engaged_sessions,
            conversions      = EXCLUDED.conversions,
            revenue          = EXCLUDED.revenue
    """
    with connection() as conn:
        with conn.cursor() as cur:
            for r in rows:
                cur.execute(sql, (
                    window_end, window_days,
                    r.get("sessionDefaultChannelGroup") or "(unknown)",
                    int(r.get("sessions") or 0),
                    int(r.get("engagedSessions") or 0),
                    round(float(r.get("conversions") or 0), 2),
                    round(float(r.get("totalRevenue") or 0), 2),
                ))
        conn.commit()
    return len(rows)


def run(days: int = ga4.DEFAULT_LOOKBACK_DAYS, all_channels: bool = False,
        show_events: bool = False, dry_run: bool = False) -> dict:
    start = time.monotonic()

    if not ga4.is_available():
        msg = ("GA4 is not configured. Set GA4_PROPERTY_ID and "
               "GA4_SERVICE_ACCOUNT_FILE in .env — see .env.example. "
               "Nothing else in the system is affected.")
        logger.warning(msg)
        print(f"\n  SKIPPED — {msg}\n")
        if not dry_run:
            record_agent_run(agent_name=AGENT_NAME, status="skipped",
                             records_processed=0, errors=[msg],
                             duration_seconds=round(time.monotonic() - start, 2),
                             metadata={"reason": "not_configured"})
        return {"status": "skipped", "reason": "not_configured"}

    window_end = date.today() - timedelta(days=ga4.DATA_LAG_DAYS)

    pages = ga4.landing_page_metrics(lookback_days=days, organic_only=not all_channels)
    channels = ga4.channel_totals(lookback_days=days)
    events = ga4.conversion_events(lookback_days=days) if show_events else None

    if pages is None and channels is None:
        err = "GA4 returned no data — check the property id and that the service account has Viewer."
        logger.error(err)
        if not dry_run:
            record_agent_run(agent_name=AGENT_NAME, status="failed",
                             records_processed=0, errors=[err],
                             duration_seconds=round(time.monotonic() - start, 2))
        return {"status": "failed", "reason": err}

    index, ambiguous = build_page_index()

    lp_stats = {"written": 0, "matched": 0, "unmatched": 0}
    ch_written = 0
    if not dry_run:
        lp_stats = upsert_landing_pages(pages or [], window_end, days, index)
        ch_written = upsert_channels(channels or [], window_end, days)
    else:
        for r in (pages or []):
            if index.get(_norm_path(r.get("landingPage") or "")):
                lp_stats["matched"] += 1
            else:
                lp_stats["unmatched"] += 1

    duration = time.monotonic() - start
    organic = next((c for c in (channels or [])
                    if (c.get("sessionDefaultChannelGroup") or "").lower() == "organic search"), None)

    if not dry_run:
        record_agent_run(
            agent_name=AGENT_NAME, status="success",
            records_processed=lp_stats["written"], errors=[],
            duration_seconds=round(duration, 2),
            metadata={
                "window_end": window_end.isoformat(), "window_days": days,
                "landing_pages": lp_stats["written"], "matched": lp_stats["matched"],
                "unmatched": lp_stats["unmatched"], "channels": ch_written,
                "all_channels": all_channels,
            },
        )

    print()
    print(f"  {'=' * 68}")
    print(f"   GA4 SYNC — window ending {window_end} ({days}d){'  [DRY RUN]' if dry_run else ''}")
    print(f"  {'=' * 68}")
    print()
    print(f"  Landing pages:     {lp_stats['written']}")
    print(f"    matched to pages:  {lp_stats['matched']}")
    print(f"    unattributed:      {lp_stats['unmatched']}"
          f"{'  (no `pages` row — run sitemap_validator)' if lp_stats['unmatched'] else ''}")
    if ambiguous:
        print(f"    ambiguous paths:   {len(ambiguous)} (same path on >1 owned domain)")
    print(f"  Channels:          {ch_written}")
    if organic:
        print(f"  Organic sessions:  {int(organic.get('sessions') or 0):,}")
        print(f"  Organic conv.:     {organic.get('conversions') or 0:,.0f}")
    if events is not None:
        print()
        print("  Conversion events firing (check this before trusting a conversion number):")
        if not events:
            print("    none — the property has no key events configured, so 'zero")
            print("    conversions' means 'not measured', not 'none happened'")
        for e in events[:10]:
            print(f"    {e.get('eventName', '?'):<34} count={int(e.get('eventCount') or 0):>8,}"
                  f"  conv={e.get('conversions') or 0:>8,.0f}")
    print(f"  Duration:          {duration:.1f}s")
    print()

    return {"status": "success", "landing_pages": lp_stats["written"],
            "matched": lp_stats["matched"], "channels": ch_written}


def main() -> None:
    parser = argparse.ArgumentParser(
        description=f"{profile().brand_name} GA4 Sync")
    parser.add_argument("--days", type=int, default=ga4.DEFAULT_LOOKBACK_DAYS,
                        help=f"Lookback window (default: {ga4.DEFAULT_LOOKBACK_DAYS})")
    parser.add_argument("--all-channels", action="store_true",
                        help="Include every channel, not just Organic Search")
    parser.add_argument("--events", action="store_true",
                        help="Also list which conversion events actually fire")
    parser.add_argument("--dry-run", action="store_true",
                        help="Fetch and report without writing")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s  %(name)s  %(levelname)s  %(message)s")
    run(days=args.days, all_channels=args.all_channels,
        show_events=args.events, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
