"""
Content Monitor — Phase 2 module of Competitive Intelligence
=============================================================

Detects new pages competitors publish, by comparing each tracked
competitor's sitemap to a manifest of URLs we've previously seen.

Why this matters
----------------
The other competitive modules detect changes once a competitor is
*already* ranking. content_monitor catches the moment they publish —
days/weeks before the new URL has a chance to be crawled by Google,
ranked, and surface as a `new_entrant` SERP event. This shortens the
"they have a new page targeting your keyword" detection cycle.

What it does
------------
For each competitor in scope:
  1. Discover their sitemap entry-point (tries /sitemap.xml,
     /sitemap_index.xml, robots.txt declaration).
  2. Walk the sitemap recursively (handles <sitemapindex>).
  3. Compare to `competitor_published_urls` rows for that competitor.
  4. For URLs not previously seen:
       - insert into competitor_published_urls (first_seen=today)
       - emit a `competitor_changes` event with change_type='new_page'
       - bump significance to 0.6 when the URL path matches one of our
         tracked keywords (means competitor is targeting a topic we
         care about)
  5. For URLs previously seen but missing from the current sitemap:
       - flag is_active=FALSE on the manifest row (don't fire `removed`
         — sitemap drop-outs are often reorganizations, too noisy)
  6. For URLs still present: bump last_seen=today.

Storage:
  - competitor_published_urls (migration 009) — the manifest
  - competitor_changes — fires new_page events for genuinely new URLs

Cost: free. HTTP-only (sitemap fetches). Per-competitor rate-limited
by the shared common.sitemap module.

Usage
-----
    # Default: primary threats only, weekly cadence
    python -m competitive_intelligence.content_monitor

    # Wider scope
    python -m competitive_intelligence.content_monitor --threat-tier primary,watch

    # One specific competitor
    python -m competitive_intelligence.content_monitor --domain itransition.com

    # Force re-scan ignoring cadence
    python -m competitive_intelligence.content_monitor --all

    # Dry run — fetch + diff but no DB writes
    python -m competitive_intelligence.content_monitor --dry-run
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

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from common.database import connection, fetch_all, record_agent_run
from common.sitemap import discover_sitemap_urls, collect_urls_from_sitemap


logger = logging.getLogger("content_monitor")
AGENT_NAME = "competitive_intelligence.content_monitor"

DEFAULT_THREAT_TIERS = ("primary",)
DEFAULT_CADENCE_DAYS = 7

# When detecting "new URL whose path matches our keyword" — used to boost
# event significance. We compile a regex from our active keyword set once
# and reuse across all URLs.
KEYWORD_PATH_BOOST_SIGNIFICANCE = 0.6
BASE_NEW_PAGE_SIGNIFICANCE      = 0.4

OUTPUT_AUDITS = Path(__file__).resolve().parent.parent / "outputs" / "audits"


# ---------------------------------------------------------------------------
# Read phase
# ---------------------------------------------------------------------------

def load_target_competitors(threat_tiers: tuple[str, ...],
                            domain: str | None,
                            cadence_days: int,
                            force_all: bool) -> list[dict]:
    """Return competitors due for a content-monitor pass."""
    if domain:
        rows = fetch_all(
            """
            SELECT id, competitor_domain, company_name, category, threat_tier,
                   (SELECT max(last_seen) FROM competitor_published_urls
                     WHERE competitor_id = c.id) AS last_scan
              FROM competitors c
             WHERE c.competitor_domain = %s AND c.is_tracked = TRUE
            """,
            [domain],
        )
        return rows

    rows = fetch_all(
        """
        SELECT id, competitor_domain, company_name, category, threat_tier,
               (SELECT max(last_seen) FROM competitor_published_urls
                 WHERE competitor_id = c.id) AS last_scan
          FROM competitors c
         WHERE c.threat_tier = ANY(%s) AND c.is_tracked = TRUE
         ORDER BY c.keyword_appearance_count DESC NULLS LAST, c.competitor_domain
        """,
        [list(threat_tiers)],
    )
    if force_all:
        return rows

    today = date.today()
    due: list[dict] = []
    for r in rows:
        if r["last_scan"] is None or (today - r["last_scan"]).days >= cadence_days:
            due.append(r)
    return due


def load_keyword_path_pattern() -> re.Pattern | None:
    """
    Compile a regex over all active keywords (slugified into URL path tokens).
    Returns None if no keywords loaded. Matches "ai-agent-development" in a
    URL like /ai-agent-development-services/.
    """
    rows = fetch_all("SELECT keyword FROM keywords WHERE status = 'active'")
    tokens: set[str] = set()
    for r in rows:
        kw = (r["keyword"] or "").strip().lower()
        if not kw:
            continue
        # Slugify: spaces -> hyphens, strip non-alphanumeric
        slug = re.sub(r"[^a-z0-9]+", "-", kw).strip("-")
        if len(slug) >= 4:   # ignore short, too-generic tokens
            tokens.add(slug)
    if not tokens:
        return None
    # Sort longest-first so multi-word slugs match before their substrings
    sorted_tokens = sorted(tokens, key=len, reverse=True)
    return re.compile("|".join(re.escape(t) for t in sorted_tokens))


def load_known_urls(competitor_id: int) -> set[str]:
    rows = fetch_all(
        "SELECT url FROM competitor_published_urls WHERE competitor_id = %s",
        [competitor_id],
    )
    return {r["url"] for r in rows}


# ---------------------------------------------------------------------------
# Process + write
# ---------------------------------------------------------------------------

def process_competitor(competitor: dict, keyword_pattern: re.Pattern | None,
                       dry_run: bool) -> dict:
    """Returns counters dict."""
    counters = {
        "competitor_domain":  competitor["competitor_domain"],
        "sitemap_found":      False,
        "sitemap_url":        None,
        "current_urls":       0,
        "known_urls":         0,
        "new_urls":           0,
        "matched_keywords":   0,
        "deactivated_urls":   0,
        "events_emitted":     0,
        "fetch_errors":       [],
        "error":              None,
    }

    candidates = discover_sitemap_urls(competitor["competitor_domain"])
    if not candidates:
        counters["error"] = "no_sitemap_found"
        logger.warning("No sitemap found for %s", competitor["competitor_domain"])
        return counters

    sitemap_url = candidates[0]
    counters["sitemap_found"] = True
    counters["sitemap_url"]   = sitemap_url
    logger.info("[%s] fetching sitemap %s", competitor["competitor_domain"], sitemap_url)

    current_urls_list, fetch_errors = collect_urls_from_sitemap(sitemap_url)
    counters["fetch_errors"] = fetch_errors
    counters["current_urls"] = len(current_urls_list)
    if not current_urls_list:
        counters["error"] = "empty_sitemap"
        return counters

    current_urls = set(current_urls_list)
    known_urls = load_known_urls(competitor["id"])
    counters["known_urls"] = len(known_urls)

    new_urls    = current_urls - known_urls
    seen_again  = current_urls & known_urls
    missing_now = known_urls - current_urls

    counters["new_urls"] = len(new_urls)

    if dry_run:
        # Still report whether the new URLs would match a Damco keyword
        if keyword_pattern:
            counters["matched_keywords"] = sum(1 for u in new_urls if keyword_pattern.search(u.lower()))
        return counters

    with connection() as conn:
        cur = conn.cursor()

        # 1. Insert new URLs into the manifest
        for url in new_urls:
            cur.execute(
                """
                INSERT INTO competitor_published_urls
                    (competitor_id, url, sitemap_source, first_seen, last_seen, is_active)
                VALUES (%s, %s, %s, CURRENT_DATE, CURRENT_DATE, TRUE)
                ON CONFLICT (competitor_id, url) DO UPDATE
                   SET last_seen = CURRENT_DATE, is_active = TRUE
                """,
                (competitor["id"], url, sitemap_url),
            )

            # Emit new_page event. Bump significance if the URL path matches
            # any of our active keyword slugs.
            keyword_match = (keyword_pattern.search(url.lower())
                             if keyword_pattern else None)
            if keyword_match:
                counters["matched_keywords"] += 1
                sig = KEYWORD_PATH_BOOST_SIGNIFICANCE
                detail = (f"New URL in sitemap: {url} "
                          f"(path token '{keyword_match.group(0)}' matches a Damco-tracked keyword)")
            else:
                sig = BASE_NEW_PAGE_SIGNIFICANCE
                detail = f"New URL in sitemap: {url}"

            cur.execute(
                """
                INSERT INTO competitor_changes
                    (competitor_id, url, change_type, diff_summary, significance_score)
                VALUES (%s, %s, 'new_page', %s, %s)
                """,
                (competitor["id"], url, detail[:2000], sig),
            )
            counters["events_emitted"] += 1

        # 2. Bump last_seen on URLs still present (no events)
        if seen_again:
            cur.execute(
                """
                UPDATE competitor_published_urls
                   SET last_seen = CURRENT_DATE, is_active = TRUE
                 WHERE competitor_id = %s AND url = ANY(%s)
                """,
                (competitor["id"], list(seen_again)),
            )

        # 3. Mark missing URLs as inactive (no event — sitemap removals are
        #    too noisy to alert on).
        if missing_now:
            cur.execute(
                """
                UPDATE competitor_published_urls SET is_active = FALSE
                 WHERE competitor_id = %s AND url = ANY(%s) AND is_active = TRUE
                """,
                (competitor["id"], list(missing_now)),
            )
            counters["deactivated_urls"] = cur.rowcount

        conn.commit()

    return counters


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def write_markdown(per_competitor: list[dict]) -> Path:
    OUTPUT_AUDITS.mkdir(parents=True, exist_ok=True)
    path = OUTPUT_AUDITS / f"content_monitor_{date.today().isoformat()}.md"

    parts: list[str] = []
    parts.append(f"# Competitor Content Monitor — {date.today().isoformat()}")
    parts.append("")
    parts.append(f"_Generated by `{AGENT_NAME}`. Detects new URLs appearing in tracked competitors' sitemaps._")
    parts.append("")

    total_new = sum(c["new_urls"] for c in per_competitor)
    total_kw  = sum(c["matched_keywords"] for c in per_competitor)
    total_events = sum(c["events_emitted"] for c in per_competitor)

    parts.append("## Summary")
    parts.append("")
    parts.append("| Metric | Value |")
    parts.append("|---|---:|")
    parts.append(f"| Competitors scanned | {len(per_competitor)} |")
    parts.append(f"| Competitors with sitemap | {sum(1 for c in per_competitor if c['sitemap_found'])} |")
    parts.append(f"| Competitors w/o sitemap | {sum(1 for c in per_competitor if not c['sitemap_found'])} |")
    parts.append(f"| Total new URLs discovered | {total_new} |")
    parts.append(f"| New URLs matching Damco keywords | {total_kw} |")
    parts.append(f"| `new_page` events emitted | {total_events} |")
    parts.append("")

    # Per-competitor table
    parts.append("## Per-competitor")
    parts.append("")
    parts.append("| Competitor | Sitemap | Current URLs | Previously Known | New URLs | Keyword-matched |")
    parts.append("|---|---|---:|---:|---:|---:|")
    for c in sorted(per_competitor, key=lambda x: -x["new_urls"]):
        sm = "✓" if c["sitemap_found"] else "✗"
        parts.append(f"| `{c['competitor_domain']}` | {sm} | {c['current_urls']} | "
                     f"{c['known_urls']} | **{c['new_urls']}** | {c['matched_keywords']} |")
    parts.append("")

    # Detail: top new URLs per competitor (limit 10)
    competitors_with_new = [c for c in per_competitor if c["new_urls"] > 0]
    if competitors_with_new:
        parts.append("## New URLs detail")
        parts.append("")
        for c in sorted(competitors_with_new, key=lambda x: -x["matched_keywords"] or -x["new_urls"]):
            # Pull the actual new URLs from DB to display
            rows = fetch_all(
                """
                SELECT url FROM competitor_published_urls
                 WHERE competitor_id = (SELECT id FROM competitors WHERE competitor_domain = %s)
                   AND first_seen = CURRENT_DATE
                 ORDER BY url LIMIT 15
                """,
                [c["competitor_domain"]],
            )
            if not rows:
                continue
            parts.append(f"### `{c['competitor_domain']}` ({c['new_urls']} new URLs, "
                         f"{c['matched_keywords']} match Damco keywords)")
            for r in rows:
                parts.append(f"- {r['url']}")
            if c["new_urls"] > 15:
                parts.append(f"- _...{c['new_urls'] - 15} more (see DB)_")
            parts.append("")

    if any(not c["sitemap_found"] for c in per_competitor):
        parts.append("## Competitors without a discoverable sitemap")
        parts.append("")
        for c in per_competitor:
            if not c["sitemap_found"]:
                parts.append(f"- `{c['competitor_domain']}`")
        parts.append("")

    path.write_text("\n".join(parts), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(threat_tiers: tuple[str, ...] = DEFAULT_THREAT_TIERS,
        domain: str | None = None,
        cadence_days: int = DEFAULT_CADENCE_DAYS,
        force_all: bool = False,
        dry_run: bool = False) -> dict:
    start = time.monotonic()

    targets = load_target_competitors(threat_tiers, domain, cadence_days, force_all)
    if not targets:
        logger.warning("No competitors due for content monitor. Use --all to force.")
        return {"status": "skipped", "reason": "no competitors due"}

    logger.info("Scanning %d competitor(s) (tiers=%s, cadence=%dd)",
                len(targets), list(threat_tiers), cadence_days)

    keyword_pattern = load_keyword_path_pattern()
    if keyword_pattern:
        logger.info("Loaded keyword-match pattern from active keywords")

    per_competitor: list[dict] = []
    for i, comp in enumerate(targets, 1):
        logger.info("[%d/%d] %s", i, len(targets), comp["competitor_domain"])
        counters = process_competitor(comp, keyword_pattern, dry_run)
        per_competitor.append(counters)

    md_path = write_markdown(per_competitor)
    duration = time.monotonic() - start

    # Aggregate
    summary = {
        "total_competitors":      len(per_competitor),
        "with_sitemap":           sum(1 for c in per_competitor if c["sitemap_found"]),
        "without_sitemap":        sum(1 for c in per_competitor if not c["sitemap_found"]),
        "total_new_urls":         sum(c["new_urls"] for c in per_competitor),
        "keyword_matched_urls":   sum(c["matched_keywords"] for c in per_competitor),
        "events_emitted":         sum(c["events_emitted"] for c in per_competitor),
        "competitors_with_new":   sum(1 for c in per_competitor if c["new_urls"] > 0),
    }

    if not dry_run:
        record_agent_run(
            agent_name=AGENT_NAME,
            status="success",
            records_processed=summary["events_emitted"],
            errors=[c["error"] for c in per_competitor if c.get("error")][:20],
            duration_seconds=round(duration, 2),
            metadata={
                "run_date":          date.today().isoformat(),
                "threat_tiers":      list(threat_tiers),
                "domain_filter":     domain,
                "cadence_days":      cadence_days,
                "force_all":         force_all,
                "summary":           summary,
                "report_path":       str(md_path),
            },
        )

    # Console
    print()
    print(f"  {'=' * 72}")
    print(f"   CONTENT MONITOR — {date.today().isoformat()}")
    print(f"  {'=' * 72}")
    print()
    print(f"  Competitors scanned:      {summary['total_competitors']}")
    print(f"  With sitemap:             {summary['with_sitemap']}")
    print(f"  Without sitemap:          {summary['without_sitemap']}")
    print(f"  Competitors with new URLs:{summary['competitors_with_new']}")
    print(f"  Total new URLs:           {summary['total_new_urls']}")
    print(f"  Keyword-matched new URLs: {summary['keyword_matched_urls']}")
    print(f"  new_page events emitted:  {summary['events_emitted']}")
    print(f"  Report:                   {md_path}")
    print(f"  Duration:                 {duration:.1f}s")
    print()

    return {
        "status":            "success",
        "summary":           summary,
        "report_path":       str(md_path),
        "duration_seconds":  round(duration, 2),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Damco Competitor Content Monitor")
    parser.add_argument("--threat-tier", default=",".join(DEFAULT_THREAT_TIERS),
                        help=f"Comma-separated tiers (default: {','.join(DEFAULT_THREAT_TIERS)})")
    parser.add_argument("--domain", help="Restrict to one competitor by domain")
    parser.add_argument("--cadence", type=int, default=DEFAULT_CADENCE_DAYS,
                        help=f"Days between scans per competitor (default: {DEFAULT_CADENCE_DAYS})")
    parser.add_argument("--all", dest="force_all", action="store_true",
                        help="Force scan ignoring cadence")
    parser.add_argument("--dry-run", action="store_true",
                        help="Fetch + diff but don't persist or emit events")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s  %(name)s  %(levelname)s  %(message)s",
    )

    tiers = tuple(t.strip() for t in args.threat_tier.split(",") if t.strip())
    invalid = [t for t in tiers if t not in ("primary", "watch", "peripheral", "ignore")]
    if invalid:
        parser.error(f"Invalid threat tiers: {invalid}")

    run(threat_tiers=tiers, domain=args.domain, cadence_days=args.cadence,
        force_all=args.force_all, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
