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
from common.tenant import profile, strip_www


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
        host = strip_www(urlparse(url).netloc.lower())
        seen.setdefault(path, set()).add(host)
        # Key on (host, path). GA4 reports which property the traffic was on,
        # so the domain is known at lookup time and the ambiguity that made
        # dropping necessary does not exist.
        index.setdefault((host, path), row["id"])
        # Keep the bare path too, for the unambiguous majority — a GA4 row
        # whose domain has no page of that path still resolves if exactly one
        # owned domain has it.
        index.setdefault(path, row["id"])

    # Paths on more than one owned domain: the bare-path key is a coin flip,
    # so remove it. The (host, path) keys stay — those are exact.
    #
    # This used to drop the path entirely, which was over-cautious and
    # expensive. `/`, `/contact-us` and `/about-us` exist on all three owned
    # domains, so the homepage — 1,938 organic sessions and 41 conversions,
    # 59% of everything the dashboard could not attribute — was thrown away
    # for want of a disambiguator that ga4_landing_pages.domain was carrying
    # all along.
    ambiguous = {path for path, hosts in seen.items() if len(hosts) > 1}
    for path in ambiguous:
        index.pop(path, None)
    if ambiguous:
        logger.info("%d path(s) exist on more than one owned domain; resolved by "
                    "domain where GA4 supplies one, dropped otherwise", len(ambiguous))
    return index, ambiguous


def lookup_page_id(index: dict, domain: str, landing_page: str) -> int | None:
    """Resolve a GA4 landing page to a `pages.id`, domain first."""
    path = _norm_path(landing_page)
    if not path:
        return None
    host = strip_www((domain or "").lower())
    return index.get((host, path)) or index.get(path)


def upsert_landing_pages(rows: list[dict], domain: str, window_end: date,
                         window_days: int, index: dict[str, int]) -> dict:
    """Upsert landing-page metrics for one property. Returns counters."""
    stats = {"written": 0, "matched": 0, "unmatched": 0}
    if not rows:
        return stats

    sql = """
        INSERT INTO ga4_landing_pages
            (domain, window_end, window_days, landing_page, channel, page_id,
             sessions, engaged_sessions, engagement_rate, conversions,
             revenue, avg_duration_sec)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (domain, window_end, window_days, landing_page, channel) DO UPDATE SET
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
                page_id = lookup_page_id(index, domain, lp)
                if page_id:
                    stats["matched"] += 1
                else:
                    stats["unmatched"] += 1
                cur.execute(sql, (
                    domain, window_end, window_days, lp[:2000],
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


def upsert_channels(rows: list[dict], domain: str, window_end: date,
                    window_days: int) -> int:
    if not rows:
        return 0
    sql = """
        INSERT INTO ga4_channel_totals
            (domain, window_end, window_days, channel, sessions,
             engaged_sessions, conversions, revenue)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (domain, window_end, window_days, channel) DO UPDATE SET
            sessions         = EXCLUDED.sessions,
            engaged_sessions = EXCLUDED.engaged_sessions,
            conversions      = EXCLUDED.conversions,
            revenue          = EXCLUDED.revenue
    """
    with connection() as conn:
        with conn.cursor() as cur:
            for r in rows:
                cur.execute(sql, (
                    domain, window_end, window_days,
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
    props = ga4.properties()
    index, ambiguous = build_page_index()

    # Domains with no GA4 property are reported, not silently omitted. "We have
    # no data for damcodigital.com" and "damcodigital.com has no traffic" look
    # identical on a dashboard and mean completely different things.
    configured = {p["domain"] for p in props}
    unconfigured = [d["domain"] for d in profile().domain_rows
                    if d["domain"] not in configured]

    print()
    print(f"  {'=' * 68}")
    print(f"   GA4 SYNC — window ending {window_end} ({days}d)"
          f"{'  [DRY RUN]' if dry_run else ''}")
    print(f"  {'=' * 68}")

    per_domain = []
    errors: list[str] = []
    total = {"written": 0, "matched": 0, "unmatched": 0, "channels": 0}

    for prop in props:
        domain, pid = prop["domain"], prop["property_id"]
        pages = ga4.landing_page_metrics(lookback_days=days,
                                        organic_only=not all_channels, prop=pid)
        channels = ga4.channel_totals(lookback_days=days, prop=pid)
        events = ga4.conversion_events(lookback_days=days, prop=pid) if show_events else None

        if pages is None and channels is None:
            err = (f"{domain} (property {pid}): no data returned — check the id and "
                   f"that the service account has Viewer on it")
            logger.error(err)
            errors.append(err)
            per_domain.append({"domain": domain, "property_id": pid, "error": err})
            continue

        lp = {"written": 0, "matched": 0, "unmatched": 0}
        ch = 0
        if dry_run:
            for r in (pages or []):
                key = ("matched" if lookup_page_id(index, domain, r.get("landingPage") or "")
                       else "unmatched")
                lp[key] += 1
        else:
            lp = upsert_landing_pages(pages or [], domain, window_end, days, index)
            ch = upsert_channels(channels or [], domain, window_end, days)

        organic = next((c for c in (channels or [])
                        if (c.get("sessionDefaultChannelGroup") or "").lower()
                        == "organic search"), None)

        for k in ("written", "matched", "unmatched"):
            total[k] += lp[k]
        total["channels"] += ch

        per_domain.append({
            "domain": domain, "property_id": pid, **lp, "channels": ch,
            "organic_sessions": int((organic or {}).get("sessions") or 0),
            "organic_conversions": float((organic or {}).get("conversions") or 0),
        })

        print()
        print(f"  {domain}  (property {pid})")
        print(f"    landing pages:     {lp['written'] or (lp['matched'] + lp['unmatched'])}")
        print(f"      matched:           {lp['matched']}")
        print(f"      unattributed:      {lp['unmatched']}"
              f"{'  (no `pages` row — run sitemap_validator)' if lp['unmatched'] else ''}")
        print(f"    channels:          {ch}")
        if organic:
            print(f"    organic sessions:  {int(organic.get('sessions') or 0):,}")
            print(f"    organic conv.:     {organic.get('conversions') or 0:,.0f}")
        if events is not None:
            print("    conversion events firing:")
            if not events:
                print("      none — this property has no key events configured, so")
                print("      'zero conversions' means 'not measured', not 'none happened'")
            for e in events[:8]:
                print(f"      {e.get('eventName', '?'):<32} "
                      f"count={int(e.get('eventCount') or 0):>8,}"
                      f"  conv={e.get('conversions') or 0:>7,.0f}")

    duration = time.monotonic() - start

    print()
    print(f"  Properties synced:   {len([p for p in per_domain if not p.get('error')])}"
          f" of {len(props)}")
    if unconfigured:
        print(f"  No GA4 property:     {', '.join(unconfigured)}"
              f"  (skipped — not the same as zero traffic)")
    if ambiguous:
        print(f"  Ambiguous paths:     {len(ambiguous)} on >1 owned domain, left unattributed")
    print(f"  Landing pages:       {total['written'] or total['matched'] + total['unmatched']}"
          f"  ({total['matched']} matched, {total['unmatched']} unattributed)")
    print(f"  Duration:            {duration:.1f}s")
    print()

    status = "success" if not errors else ("partial" if total["written"] else "failed")
    if not dry_run:
        record_agent_run(
            agent_name=AGENT_NAME, status=status,
            records_processed=total["written"], errors=errors[:5],
            duration_seconds=round(duration, 2),
            metadata={
                "window_end": window_end.isoformat(), "window_days": days,
                "properties": len(props), "per_domain": per_domain,
                "unconfigured_domains": unconfigured,
                "all_channels": all_channels, **total,
            },
        )

    return {"status": status, "properties": len(props), "per_domain": per_domain, **total}


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
