"""
Competitor Monitor — Phase 2 module of Competitive Intelligence
================================================================

Periodically crawls tracked competitor URLs and detects on-page changes:
title rewrites, meta description rewrites, H1 changes, schema markup
changes, content rewrites. Each detected change becomes a row in
`competitor_changes` so the operations team can spot what competitors
are doing differently *between* our rank-tracker cycles.

Detection inputs
----------------
For each (competitor_id, url) pair to monitor:
  1. Fetch the URL via the shared crawler connector.
  2. Compare key fields against `competitor_pages` (previous snapshot).
  3. Update `competitor_pages` with the new state.
  4. Insert a `competitor_changes` row for each meaningful delta.

Change taxonomy (matches the CHECK constraint on competitor_changes):
  new_page          — URL not previously in competitor_pages
  removed           — URL now returns 4xx/5xx
  title_change      — <title> differs from stored value
  meta_change       — meta description differs
  structure_change  — h1 OR schema_types changed
  content_update    — body content_hash differs (no other field changed)

Significance scores (0.00 - 1.00):
  title_change       0.70  (Google rewrites SERP snippet on title change)
  structure_change   0.60  (H1 + schema are ranking signals)
  meta_change        0.50
  content_update     0.40  (could be cosmetic; scale by word_count delta)
  new_page           0.40
  removed            0.50

Scope filter — by default, crawls top-10 URLs of primary + watch tier
competitors only. Override via --threat-tier.

Usage
-----
    # Default: primary + watch competitors, top-10 URLs, all offerings
    python -m competitive_intelligence.competitor_monitor

    # Only primary threats (smaller scope, runs faster)
    python -m competitive_intelligence.competitor_monitor --threat-tier primary

    # One offering
    python -m competitive_intelligence.competitor_monitor --offering "AI"

    # Cadence: skip URLs crawled in the last N days (default 7)
    python -m competitive_intelligence.competitor_monitor --cadence 14

    # Force re-crawl ignoring cadence
    python -m competitive_intelligence.competitor_monitor --all

    # Dry run — crawl + diff but don't persist
    python -m competitive_intelligence.competitor_monitor --dry-run
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, date
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from common.connectors.crawler import Crawler, CrawlResult
from common.database import connection, fetch_all, record_agent_run


logger = logging.getLogger("competitor_monitor")
AGENT_NAME = "competitive_intelligence.competitor_monitor"

DEFAULT_THREAT_TIERS = ("primary", "watch")
DEFAULT_CADENCE_DAYS = 7
DEFAULT_WORKERS = 4
DEFAULT_TOP_N = 10   # Only monitor URLs that ranked top-N for any of our keywords

SIGNIFICANCE = {
    "title_change":     0.70,
    "structure_change": 0.60,
    "meta_change":      0.50,
    "removed":          0.50,
    "content_update":   0.40,
    "new_page":         0.40,
}


# ---------------------------------------------------------------------------
# Read phase — what to crawl
# ---------------------------------------------------------------------------

def load_urls_to_monitor(threat_tiers: tuple[str, ...],
                        offering: str | None,
                        top_n: int,
                        cadence_days: int,
                        force_all: bool) -> list[dict]:
    """
    Returns [{competitor_id, competitor_domain, url}] — one row per
    (competitor, url) pair worth monitoring. Filtered by threat tier,
    offering scope, top-N positions, and per-URL cadence.

    Source of truth for URLs is `competitor_rankings` (the latest snapshot
    per keyword). Cadence is per-URL: if competitor_pages.last_fetched_at
    is within `cadence_days`, we skip the URL.
    """
    params: list[Any] = [list(threat_tiers), top_n]
    sql = """
        WITH latest AS (
            SELECT keyword_id, max(date) AS d FROM competitor_rankings
             GROUP BY keyword_id
        )
        SELECT DISTINCT
               c.id                AS competitor_id,
               c.competitor_domain AS competitor_domain,
               cr.url_found        AS url
          FROM competitor_rankings cr
          JOIN latest l ON l.keyword_id = cr.keyword_id AND l.d = cr.date
          JOIN competitors c ON c.id = cr.competitor_id
          JOIN keywords k ON k.id = cr.keyword_id
         WHERE c.is_tracked = TRUE
           AND c.threat_tier = ANY(%s)
           AND cr.rank_position BETWEEN 1 AND %s
           AND cr.url_found IS NOT NULL
           AND k.status = 'active'
    """
    if offering:
        sql += " AND k.offering = %s"
        params.append(offering)
    rows = fetch_all(sql, params)

    if force_all:
        return rows

    # Cadence filter: drop URLs whose competitor_pages.last_fetched_at is recent
    cur_known = {
        (r["competitor_id"], r["url"]): r["last_fetched_at"]
        for r in fetch_all(
            "SELECT competitor_id, url, last_fetched_at FROM competitor_pages "
            "WHERE last_fetched_at IS NOT NULL"
        )
    }
    today_utc = datetime.now(timezone.utc)
    due: list[dict] = []
    for r in rows:
        last = cur_known.get((r["competitor_id"], r["url"]))
        if last is None or (today_utc - last).days >= cadence_days:
            due.append(r)
    return due


def load_previous_state(competitor_id: int, url: str) -> dict | None:
    rows = fetch_all(
        """
        SELECT id, title, meta_description, h1, canonical_url, lang,
               word_count, schema_types, has_microdata, content_hash,
               last_status, last_fetched_at
          FROM competitor_pages
         WHERE competitor_id = %s AND url = %s
         LIMIT 1
        """,
        [competitor_id, url],
    )
    return rows[0] if rows else None


# ---------------------------------------------------------------------------
# Process phase — crawl + diff
# ---------------------------------------------------------------------------

def fetch_one(crawler: Crawler, target: dict) -> tuple[dict, CrawlResult]:
    """Worker function passed to ThreadPoolExecutor."""
    try:
        result = crawler.fetch(target["url"])
    except Exception as exc:
        result = CrawlResult(url=target["url"], error=str(exc))
    return target, result


def crawl_parallel(targets: list[dict], crawler: Crawler,
                   workers: int) -> list[tuple[dict, CrawlResult]]:
    out: list[tuple[dict, CrawlResult]] = []
    if not targets:
        return out
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(fetch_one, crawler, t): t for t in targets}
        for i, fut in enumerate(as_completed(futures), 1):
            if i % 25 == 0 or i == len(targets):
                logger.info("  crawled %d/%d", i, len(targets))
            out.append(fut.result())
    return out


def extract_state(r: CrawlResult) -> dict:
    """Distill a CrawlResult into the fields competitor_pages stores."""
    if r.error or not r.is_html:
        return {
            "title": None, "meta_description": None, "h1": None,
            "canonical_url": None, "lang": None,
            "word_count": None, "schema_types": [], "has_microdata": False,
            "content_hash": None,
        }
    schema_types: set[str] = set()
    for block in (r.schema_jsonld or []):
        entities = block.get("@graph") if isinstance(block, dict) and "@graph" in block else [block]
        for e in entities or []:
            if isinstance(e, dict):
                t = e.get("@type")
                if isinstance(t, list):
                    for tt in t:
                        if isinstance(tt, str): schema_types.add(tt)
                elif isinstance(t, str):
                    schema_types.add(t)

    # Hash of visible text (already extracted by crawler; reproduce minimally)
    content_for_hash = (r.title or "") + "|" + (r.meta_description or "") + "|" + " ".join(r.h1_tags or [])
    # Best signal of body change: word_count + first 200 chars of body via re-hash of HTML body
    # Here we use a small but stable proxy: title + meta + h1s + word_count bucket
    body_proxy = f"wc={r.word_count}|" + content_for_hash
    content_hash = hashlib.sha256(body_proxy.encode("utf-8", errors="replace")).hexdigest()

    return {
        "title":            r.title,
        "meta_description": r.meta_description,
        "h1":               r.h1_tags[0] if r.h1_tags else None,
        "canonical_url":    r.canonical,
        "lang":             r.lang,
        "word_count":       r.word_count,
        "schema_types":     sorted(schema_types),
        "has_microdata":    r.has_microdata,
        "content_hash":     content_hash,
    }


def diff_state(prev: dict | None, curr: dict, result: CrawlResult) -> list[dict]:
    """
    Returns list of change dicts: {change_type, diff_summary, significance}.

    Status handling:
      - 404 / 410           → real "removed" signal (fire event if had prev state)
      - 403 / 401           → bot-blocked or auth-walled (no event — not a change)
      - 5xx                 → transient server error (no event — retry next cycle)
      - Other 4xx           → ambiguous; treated as removed only if prev existed
      - Transport error     → our side / network (no event)
      - HTML 2xx            → normal diff path
    """
    # Transport-level fetch errors: never fire events (transient).
    if result.error:
        return []

    status = result.status or 0
    if status in (404, 410):
        if prev is None:
            return []   # Never had it, never lost it
        return [{
            "change_type":  "removed",
            "diff_summary": f"URL returned status={status} (gone)",
            "significance": SIGNIFICANCE["removed"],
        }]
    if status == 403 or status == 401:
        # Crawler-blocked / auth-walled. Page may still exist; don't pollute the
        # change stream. Operational note logged at the run level instead.
        return []
    if 500 <= status < 600:
        return []   # transient server error
    if status >= 400:
        # Other 4xx (e.g. 400, 405, 451). Treat as removed only if we had state.
        if prev is None:
            return []
        return [{
            "change_type":  "removed",
            "diff_summary": f"URL returned status={status}",
            "significance": SIGNIFICANCE["removed"],
        }]

    if not curr["title"] and not curr["content_hash"]:
        # Non-HTML (e.g. PDF) or empty parse — nothing to diff
        return []

    if prev is None:
        return [{
            "change_type":  "new_page",
            "diff_summary": f"First crawl. title={curr['title']!r}, words={curr['word_count']}",
            "significance": SIGNIFICANCE["new_page"],
        }]

    events: list[dict] = []

    # Title change
    if (prev.get("title") or "") != (curr["title"] or ""):
        events.append({
            "change_type":  "title_change",
            "diff_summary": f"{prev.get('title')!r} -> {curr['title']!r}",
            "significance": SIGNIFICANCE["title_change"],
        })

    # Meta description change
    if (prev.get("meta_description") or "") != (curr["meta_description"] or ""):
        events.append({
            "change_type":  "meta_change",
            "diff_summary": f"meta_description changed (len {len(prev.get('meta_description') or '')} -> "
                            f"{len(curr['meta_description'] or '')})",
            "significance": SIGNIFICANCE["meta_change"],
        })

    # Structure change: H1 or schema types
    h1_changed     = (prev.get("h1") or "") != (curr["h1"] or "")
    prev_schemas   = set((prev.get("schema_types") or []))
    curr_schemas   = set(curr["schema_types"])
    schema_changed = prev_schemas != curr_schemas
    if h1_changed or schema_changed:
        parts = []
        if h1_changed:
            parts.append(f"h1: {prev.get('h1')!r} -> {curr['h1']!r}")
        if schema_changed:
            parts.append(f"schema_types added: {sorted(curr_schemas - prev_schemas)}, "
                         f"removed: {sorted(prev_schemas - curr_schemas)}")
        events.append({
            "change_type":  "structure_change",
            "diff_summary": "; ".join(parts),
            "significance": SIGNIFICANCE["structure_change"],
        })

    # Content update — only fires if no other change above triggered, since
    # content_hash inherently changes when title/meta/h1 do.
    if not events:
        if (prev.get("content_hash") or "") != (curr["content_hash"] or ""):
            wc_prev = prev.get("word_count") or 0
            wc_curr = curr["word_count"] or 0
            wc_pct = abs(wc_curr - wc_prev) / max(1, wc_prev) if wc_prev else 0
            sig = SIGNIFICANCE["content_update"]
            if wc_pct >= 0.20:
                sig = min(0.80, sig + 0.30)  # big body change
            elif wc_pct >= 0.05:
                sig = sig + 0.10
            events.append({
                "change_type":  "content_update",
                "diff_summary": f"content hash differs (word_count {wc_prev} -> {wc_curr})",
                "significance": round(sig, 2),
            })

    return events


# ---------------------------------------------------------------------------
# Write phase
# ---------------------------------------------------------------------------

def upsert_competitor_page(cur, target: dict, state: dict, result: CrawlResult) -> None:
    cur.execute(
        """
        INSERT INTO competitor_pages
            (competitor_id, url, title, meta_description, h1, canonical_url, lang,
             word_count, schema_types, has_microdata, content_hash, last_status,
             last_fetched_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, now())
        ON CONFLICT (competitor_id, url) DO UPDATE SET
            title            = EXCLUDED.title,
            meta_description = EXCLUDED.meta_description,
            h1               = EXCLUDED.h1,
            canonical_url    = EXCLUDED.canonical_url,
            lang             = EXCLUDED.lang,
            word_count       = EXCLUDED.word_count,
            schema_types     = EXCLUDED.schema_types,
            has_microdata    = EXCLUDED.has_microdata,
            content_hash     = EXCLUDED.content_hash,
            last_status      = EXCLUDED.last_status,
            last_fetched_at  = now()
        """,
        (
            target["competitor_id"], target["url"],
            state["title"], state["meta_description"], state["h1"],
            state["canonical_url"], state["lang"], state["word_count"],
            json.dumps(state["schema_types"]), state["has_microdata"],
            state["content_hash"], result.status,
        ),
    )


def insert_change(cur, competitor_id: int, url: str, event: dict) -> None:
    cur.execute(
        """
        INSERT INTO competitor_changes (competitor_id, url, change_type,
                                        diff_summary, significance_score)
        VALUES (%s, %s, %s, %s, %s)
        """,
        (competitor_id, url, event["change_type"],
         event["diff_summary"][:2000], event["significance"]),
    )


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def process_results(crawl_pairs: list[tuple[dict, CrawlResult]],
                    dry_run: bool) -> dict:
    counters = {
        "pages_processed":    0,
        "fetch_errors":       0,
        "pages_upserted":     0,
        "changes_recorded":   0,
        "by_change_type":     {},
    }

    if not crawl_pairs:
        return counters

    if dry_run:
        for target, result in crawl_pairs:
            counters["pages_processed"] += 1
            if result.error or not result.is_html:
                counters["fetch_errors"] += 1
                continue
            prev = load_previous_state(target["competitor_id"], target["url"])
            curr = extract_state(result)
            for ev in diff_state(prev, curr, result):
                counters["changes_recorded"] += 1
                counters["by_change_type"][ev["change_type"]] = \
                    counters["by_change_type"].get(ev["change_type"], 0) + 1
        return counters

    with connection() as conn:
        cur = conn.cursor()
        for target, result in crawl_pairs:
            counters["pages_processed"] += 1
            if result.error or not result.is_html:
                counters["fetch_errors"] += 1
                # Even on error, see if this used to be there (potential "removed")
                prev = load_previous_state(target["competitor_id"], target["url"])
                for ev in diff_state(prev, extract_state(result), result):
                    insert_change(cur, target["competitor_id"], target["url"], ev)
                    counters["changes_recorded"] += 1
                    counters["by_change_type"][ev["change_type"]] = \
                        counters["by_change_type"].get(ev["change_type"], 0) + 1
                conn.commit()
                continue

            prev  = load_previous_state(target["competitor_id"], target["url"])
            curr  = extract_state(result)
            events = diff_state(prev, curr, result)

            upsert_competitor_page(cur, target, curr, result)
            counters["pages_upserted"] += 1

            for ev in events:
                insert_change(cur, target["competitor_id"], target["url"], ev)
                counters["changes_recorded"] += 1
                counters["by_change_type"][ev["change_type"]] = \
                    counters["by_change_type"].get(ev["change_type"], 0) + 1

            conn.commit()

    return counters


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(threat_tiers: tuple[str, ...] = DEFAULT_THREAT_TIERS,
        offering: str | None = None,
        top_n: int = DEFAULT_TOP_N,
        cadence_days: int = DEFAULT_CADENCE_DAYS,
        workers: int = DEFAULT_WORKERS,
        force_all: bool = False,
        dry_run: bool = False) -> dict:
    start = time.monotonic()

    targets = load_urls_to_monitor(threat_tiers, offering, top_n, cadence_days, force_all)
    if not targets:
        logger.warning(
            "No URLs are due for monitoring. threat_tiers=%s, offering=%s, "
            "cadence=%dd, force_all=%s. Use --all to force.",
            list(threat_tiers), offering, cadence_days, force_all,
        )
        return {"status": "skipped", "reason": "no urls due"}

    logger.info(
        "Monitoring %d (competitor, url) pair(s)  [tiers=%s, offering=%s, top_n=%d, workers=%d]",
        len(targets), list(threat_tiers), offering, top_n, workers,
    )

    crawler = Crawler()
    crawl_pairs = crawl_parallel(targets, crawler, workers)
    counters = process_results(crawl_pairs, dry_run)
    duration = time.monotonic() - start

    if not dry_run:
        record_agent_run(
            agent_name=AGENT_NAME,
            status="success" if counters["fetch_errors"] == 0 else "partial",
            records_processed=counters["pages_upserted"],
            errors=[],
            duration_seconds=round(duration, 2),
            metadata={
                "run_date":          date.today().isoformat(),
                "threat_tiers":      list(threat_tiers),
                "offering":          offering,
                "top_n":             top_n,
                "cadence_days":      cadence_days,
                "force_all":         force_all,
                "urls_targeted":     len(targets),
                "pages_processed":   counters["pages_processed"],
                "pages_upserted":    counters["pages_upserted"],
                "fetch_errors":      counters["fetch_errors"],
                "changes_recorded":  counters["changes_recorded"],
                "changes_by_type":   counters["by_change_type"],
            },
        )

    # Summary
    print()
    print(f"  {'=' * 72}")
    print(f"   COMPETITOR MONITOR — {date.today().isoformat()}")
    print(f"  {'=' * 72}")
    print()
    print(f"  URLs targeted:        {len(targets)}")
    print(f"  Pages processed:      {counters['pages_processed']}")
    print(f"  Fetch errors:         {counters['fetch_errors']}")
    print(f"  Pages upserted:       {counters['pages_upserted']}")
    print(f"  Change events:        {counters['changes_recorded']}")
    if counters["by_change_type"]:
        print(f"  Changes by type:")
        for ct, n in sorted(counters["by_change_type"].items(), key=lambda x: -x[1]):
            print(f"    {ct:<24}  {n}")
    print(f"  Duration:             {duration:.1f}s")
    print()

    return {
        "status":           "success" if counters["fetch_errors"] == 0 else "partial",
        "duration_seconds": round(duration, 2),
        "urls_targeted":    len(targets),
        "pages_upserted":   counters["pages_upserted"],
        "changes_recorded": counters["changes_recorded"],
        "by_change_type":   counters["by_change_type"],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Damco Competitor Monitor")
    parser.add_argument("--threat-tier", default=",".join(DEFAULT_THREAT_TIERS),
                        help=f"Comma-separated tiers (default: {','.join(DEFAULT_THREAT_TIERS)}). "
                             "Allowed: primary, watch, peripheral, ignore.")
    parser.add_argument("--offering", help="Restrict to one offering")
    parser.add_argument("--top-n", type=int, default=DEFAULT_TOP_N,
                        help=f"Only monitor URLs that ranked top-N (default: {DEFAULT_TOP_N})")
    parser.add_argument("--cadence", type=int, default=DEFAULT_CADENCE_DAYS,
                        help=f"Days between re-crawls per URL (default: {DEFAULT_CADENCE_DAYS})")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS,
                        help=f"Parallel crawler workers (default: {DEFAULT_WORKERS})")
    parser.add_argument("--all", dest="force_all", action="store_true",
                        help="Force re-crawl ignoring cadence")
    parser.add_argument("--dry-run", action="store_true",
                        help="Crawl + diff but don't write to DB")
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

    run(threat_tiers=tiers, offering=args.offering, top_n=args.top_n,
        cadence_days=args.cadence, workers=args.workers,
        force_all=args.force_all, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
