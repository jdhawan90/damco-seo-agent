"""
Keyword Rank Tracker Agent — Phase 1
=====================================

Standard agent lifecycle:
  Read    — fetch active keywords from DB (respecting fortnightly cadence),
            call DataForSEO SERP API
  Process — find Damco positions, compute buckets, extract SERP features,
            diff vs previous snapshot
  Write   — upsert keyword_rankings (Damco-side), keyword_serp_snapshots,
            competitor_rankings, competitors (auto-stub), competitor_serp_events;
            recompute competitor aggregates; refresh mv_offering_competition.
  Notify  — console summary (Slack/email TBD)

Usage
-----
    # Track keywords whose latest snapshot is older than their snapshot_frequency_days
    python -m keyword_intelligence.rank_tracker

    # Track only a specific offering (still respects cadence)
    python -m keyword_intelligence.rank_tracker --offering "AI"

    # Force a snapshot for ALL active keywords (ignores cadence)
    python -m keyword_intelligence.rank_tracker --all

    # Use live queue for immediate results (~3x more expensive)
    python -m keyword_intelligence.rank_tracker --queue live

    # Dry run — call the API but don't write to DB
    python -m keyword_intelligence.rank_tracker --dry-run
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
from datetime import date
from pathlib import Path

import psycopg2.extras

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

# Top N for competition tracking (separate concern from Damco brand match,
# which scans the full SERP).
TOP_N = 10

# Heuristic categorization buckets. Default-NULL when uncertain — humans curate.
BIG_TECH_DOMAINS = {
    "cloud.google.com", "google.com", "developers.google.com",
    "aws.amazon.com", "amazon.com",
    "learn.microsoft.com", "microsoft.com", "azure.microsoft.com",
    "developer.apple.com", "apple.com",
    "openai.com", "anthropic.com",
    "meta.com", "developers.facebook.com",
}

KNOWN_AGGREGATORS = {
    "g2.com", "capterra.com", "gartner.com", "forrester.com",
    "trustradius.com", "softwareadvice.com",
    "clutch.co", "goodfirms.co", "designrush.com",
    "forbes.com", "techcrunch.com",
}

INFORMATIONAL_DOMAINS = {
    "wikipedia.org", "en.wikipedia.org",
    "youtube.com", "reddit.com", "quora.com", "medium.com",
}


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
    """First Damco-domain item in a list of organic SERP items, or None."""
    for item in serp_items:
        domain = (item.get("domain") or "").lower()
        for brand in BRAND_DOMAINS:
            if brand in domain:
                return item
    return None


def extract_serp_features(raw: dict | None) -> tuple[list[str], bool, list[dict]]:
    """
    Walk the raw DataForSEO result and pull SERP feature presence.

    Returns:
        features              -- sorted list of non-organic item types seen
        ai_overview_present   -- bool
        ai_overview_citations -- list of {domain, url, title} cited inside AI Overview
    """
    if not raw:
        return [], False, []

    features: set[str] = set()
    ai_present = False
    ai_citations: list[dict] = []

    for item in (raw.get("items") or []):
        item_type = item.get("type") or ""
        if item_type and item_type != "organic":
            features.add(item_type)

        if item_type == "ai_overview":
            ai_present = True
            # Citation shape varies by DataForSEO API version. Look in multiple places.
            for nested_key in ("references", "items", "links", "citations"):
                for ref in (item.get(nested_key) or []):
                    if not isinstance(ref, dict):
                        continue
                    citation = {
                        "domain": ref.get("domain"),
                        "url": ref.get("url"),
                        "title": ref.get("title"),
                    }
                    if citation["domain"] or citation["url"]:
                        ai_citations.append(citation)

    return sorted(features), ai_present, ai_citations


_LISTICLE_PATTERN = re.compile(
    r"\b(top|best)\s*(\d+|\w+)?\s*(ai|companies|firms|providers|tools|services)\b",
    re.IGNORECASE,
)


def categorize_page_type(url: str | None, title: str | None, domain: str | None) -> str:
    """Best-effort page-type label. Conservative — defaults to 'unknown'."""
    url = (url or "").lower()
    title = (title or "").lower()
    domain = (domain or "").lower()

    if not url:
        return "unknown"

    # Homepage: just domain or domain/
    path = url.split(domain, 1)[-1] if domain else ""
    if path in ("", "/", "/index.html"):
        return "homepage"

    if domain in BIG_TECH_DOMAINS or domain.startswith("docs."):
        return "docs"
    if "/blog/" in url or "/insights/" in url or "/articles/" in url:
        return "blog"
    if _LISTICLE_PATTERN.search(title) or "/top-" in url or "/best-" in url:
        return "listicle"
    if re.search(r"\b(services|solutions|consulting|development|automation)\b", url):
        return "service"
    return "unknown"


def categorize_competitor(domain: str | None) -> str | None:
    """Cheap heuristic. Returns None when uncertain — humans curate."""
    if not domain:
        return None
    d = domain.lower()
    if d in BRAND_DOMAINS:
        return "internal"
    if d in BIG_TECH_DOMAINS:
        return "big_tech"
    if d in INFORMATIONAL_DOMAINS or d.endswith(".wikipedia.org"):
        return "informational"
    if d in KNOWN_AGGREGATORS:
        return "aggregator"
    return None  # let humans set 'direct' / 'adjacent' on review


def severity_for(event_type: str, value: int | None = None) -> str:
    """
    Severity rules from sql/DESIGN_competition_tracking.md.

    `value` semantics by event_type:
        new_entrant      → position (1-10)
        drop_out         → previous_position (1-10)
        position_gain    → abs(delta) -- magnitude only
        position_drop    → abs(delta) -- magnitude only
        damco_position_change → signed delta (positive = drop, negative = gain)
    """
    if event_type == "new_entrant":
        if value is not None and value <= 5:
            return "high"
        if value is not None and value <= 10:
            return "medium"
        return "low"
    if event_type == "drop_out":
        if value is not None and value <= 5:
            return "medium"
        return "low"
    if event_type in ("position_gain", "position_drop"):
        if value is not None and value >= 5:
            return "high"
        if value is not None and value >= 3:
            return "medium"
        return "low"
    if event_type == "damco_drops_top_n":
        return "critical"
    if event_type == "damco_enters_top_n":
        return "high"
    if event_type == "damco_position_change":
        if value is None:
            return "info"
        if value >= 5:
            return "high"   # drop of 5+
        if value >= 3:
            return "medium" # drop of 3-4
        return "info"       # gain or small drop
    if event_type in ("serp_feature_appeared", "serp_feature_disappeared"):
        return "medium"
    return "info"


# ---------------------------------------------------------------------------
# Read phase
# ---------------------------------------------------------------------------

def load_keywords(offering: str | None = None, only_due: bool = True) -> list[dict]:
    """
    Fetch active keywords. By default returns only those due for a snapshot
    based on `keywords.snapshot_frequency_days` and the latest entry in
    `keyword_serp_snapshots`. Pass only_due=False to force every keyword.
    """
    if only_due:
        sql = """
            SELECT k.id, k.keyword, k.offering, k.target_url, k.snapshot_frequency_days,
                   (SELECT MAX(date) FROM keyword_serp_snapshots
                     WHERE keyword_id = k.id AND device = 'desktop') AS last_snapshot
              FROM keywords k
             WHERE k.status = 'active'
        """
        params: list = []
        if offering:
            sql += " AND k.offering = %s"
            params.append(offering)
        sql += " ORDER BY k.offering, k.keyword"
        rows = fetch_all(sql, params)
        today = date.today()
        due: list[dict] = []
        for r in rows:
            last = r.get("last_snapshot")
            freq = r.get("snapshot_frequency_days") or 14
            if last is None or (today - last).days >= freq:
                due.append(r)
        return due

    sql = """
        SELECT id, keyword, offering, target_url, snapshot_frequency_days
          FROM keywords WHERE status = 'active'
    """
    params = []
    if offering:
        sql += " AND offering = %s"
        params.append(offering)
    sql += " ORDER BY offering, keyword"
    return fetch_all(sql, params)


def load_previous_snapshot(cur, keyword_id: int) -> dict | None:
    """Latest snapshot for this keyword (before today). Returns top_10 + features + damco_position."""
    cur.execute(
        """
        SELECT date, serp_features, ai_overview_present, top_10_domains, damco_position
          FROM keyword_serp_snapshots
         WHERE keyword_id = %s AND device = 'desktop' AND date < CURRENT_DATE
         ORDER BY date DESC LIMIT 1
        """,
        (keyword_id,),
    )
    row = cur.fetchone()
    if not row:
        return None

    # Pull the previous top 10 with positions.
    cur.execute(
        """
        SELECT cr.rank_position, c.competitor_domain
          FROM competitor_rankings cr
          JOIN competitors c ON c.id = cr.competitor_id
         WHERE cr.keyword_id = %s AND cr.date = %s
           AND cr.rank_position BETWEEN 1 AND %s
         ORDER BY cr.rank_position
        """,
        (keyword_id, row["date"] if isinstance(row, dict) else row[0], TOP_N),
    )
    prev_top = [{"rank_position": r[0] if not isinstance(r, dict) else r["rank_position"],
                 "domain":        r[1] if not isinstance(r, dict) else r["competitor_domain"]}
                for r in cur.fetchall()]

    # row may be a RealDictRow or a plain tuple depending on cursor factory.
    if isinstance(row, dict):
        return {
            "date":                row["date"],
            "serp_features":       row["serp_features"] or [],
            "ai_overview_present": row["ai_overview_present"],
            "damco_position":      row["damco_position"],
            "top_10":              prev_top,
        }
    return {
        "date":                row[0],
        "serp_features":       row[1] or [],
        "ai_overview_present": row[2],
        "damco_position":      row[4],
        "top_10":              prev_top,
    }


# ---------------------------------------------------------------------------
# Process phase
# ---------------------------------------------------------------------------

def fetch_rankings(keywords: list[dict], queue: str) -> dict[int, dict]:
    """
    Call DataForSEO for all keywords in batches.
    Returns {keyword_id: {keyword, items, raw, error}}.
    """
    out: dict[int, dict] = {}
    keyword_texts = [kw["keyword"] for kw in keywords]
    kw_id_map = {kw["keyword"]: kw["id"] for kw in keywords}

    for i in range(0, len(keyword_texts), BATCH_SIZE):
        batch = keyword_texts[i : i + BATCH_SIZE]
        batch_num = (i // BATCH_SIZE) + 1
        total_batches = (len(keyword_texts) + BATCH_SIZE - 1) // BATCH_SIZE
        logger.info("Batch %d/%d: %d keywords", batch_num, total_batches, len(batch))

        try:
            serp_results = get_serp_rankings(batch, queue=queue)
        except DataForSEOError as exc:
            logger.error("DataForSEO batch %d failed: %s", batch_num, exc)
            for kw_text in batch:
                kid = kw_id_map.get(kw_text)
                if kid is not None:
                    out[kid] = {"keyword": kw_text, "items": [], "raw": None, "error": str(exc)}
            continue

        for serp in serp_results:
            kid = kw_id_map.get(serp["keyword"])
            if kid is None:
                continue
            out[kid] = {
                "keyword": serp["keyword"],
                "items":   serp.get("items") or [],
                "raw":     serp.get("raw"),
                "error":   None,
            }

    return out


def diff_top_n(previous_top: list[dict], current_top: list[dict]) -> list[dict]:
    """
    Compare two top-N lists and return event records.

    Each input item: {"domain": "...", "rank_position": int}
    Each output event has: event_type, domain, old_value, new_value, delta, severity.
    """
    events: list[dict] = []
    prev_by_domain = {x["domain"]: x for x in previous_top if x.get("domain")}
    curr_by_domain = {x["domain"]: x for x in current_top  if x.get("domain")}

    for domain, curr in curr_by_domain.items():
        prev = prev_by_domain.get(domain)
        cpos = curr["rank_position"]
        if prev is None:
            events.append({
                "event_type": "new_entrant",
                "domain":     domain,
                "old_value":  {},
                "new_value":  {"position": cpos},
                "delta":      None,
                "severity":   severity_for("new_entrant", cpos),
            })
        else:
            ppos = prev["rank_position"]
            delta = cpos - ppos  # positive = moved down (worse), negative = moved up (better)
            if delta <= -3:
                events.append({
                    "event_type": "position_gain",
                    "domain":     domain,
                    "old_value":  {"position": ppos},
                    "new_value":  {"position": cpos},
                    "delta":      delta,
                    "severity":   severity_for("position_gain", abs(delta)),
                })
            elif delta >= 3:
                events.append({
                    "event_type": "position_drop",
                    "domain":     domain,
                    "old_value":  {"position": ppos},
                    "new_value":  {"position": cpos},
                    "delta":      delta,
                    "severity":   severity_for("position_drop", delta),
                })

    for domain, prev in prev_by_domain.items():
        if domain in curr_by_domain:
            continue
        ppos = prev["rank_position"]
        events.append({
            "event_type": "drop_out",
            "domain":     domain,
            "old_value":  {"position": ppos},
            "new_value":  {},
            "delta":      None,
            "severity":   severity_for("drop_out", ppos),
        })

    return events


def diff_damco(prev_pos: int | None, curr_pos: int | None) -> list[dict]:
    events: list[dict] = []
    in_top = lambda p: p is not None and p <= TOP_N
    if in_top(curr_pos) and not in_top(prev_pos):
        events.append({
            "event_type": "damco_enters_top_n",
            "old_value":  {"position": prev_pos},
            "new_value":  {"position": curr_pos},
            "delta":      None,
            "severity":   severity_for("damco_enters_top_n"),
        })
    if in_top(prev_pos) and not in_top(curr_pos):
        events.append({
            "event_type": "damco_drops_top_n",
            "old_value":  {"position": prev_pos},
            "new_value":  {"position": curr_pos},
            "delta":      None,
            "severity":   severity_for("damco_drops_top_n"),
        })
    if prev_pos is not None and curr_pos is not None and prev_pos != curr_pos:
        delta = curr_pos - prev_pos
        events.append({
            "event_type": "damco_position_change",
            "old_value":  {"position": prev_pos},
            "new_value":  {"position": curr_pos},
            "delta":      delta,
            "severity":   severity_for("damco_position_change", delta),
        })
    return events


def diff_serp_features(prev: list[str], curr: list[str]) -> list[dict]:
    prev_set = set(prev or [])
    curr_set = set(curr or [])
    events = []
    for feat in curr_set - prev_set:
        events.append({
            "event_type": "serp_feature_appeared",
            "old_value":  {},
            "new_value":  {"feature": feat},
            "delta":      None,
            "severity":   severity_for("serp_feature_appeared"),
        })
    for feat in prev_set - curr_set:
        events.append({
            "event_type": "serp_feature_disappeared",
            "old_value":  {"feature": feat},
            "new_value":  {},
            "delta":      None,
            "severity":   severity_for("serp_feature_disappeared"),
        })
    return events


# ---------------------------------------------------------------------------
# Write phase
# ---------------------------------------------------------------------------

def upsert_competitor(cur, domain: str, today: date) -> int:
    """Insert-or-update a competitor stub. Returns competitor_id."""
    category = categorize_competitor(domain)
    cur.execute(
        """
        INSERT INTO competitors
            (competitor_domain, category, first_seen_date, last_seen_date, status, is_tracked)
        VALUES (%s, %s, %s, %s, 'active', TRUE)
        ON CONFLICT (competitor_domain) DO UPDATE
        SET last_seen_date = GREATEST(competitors.last_seen_date, EXCLUDED.last_seen_date),
            category       = COALESCE(competitors.category, EXCLUDED.category)
        RETURNING id
        """,
        (domain, category, today, today),
    )
    return cur.fetchone()[0]


def write_keyword_serp_snapshot(
    cur, *, keyword_id: int, run_date: date, serp_features: list[str],
    ai_present: bool, ai_citations: list[dict], total_results: int,
    damco_position: int | None, damco_url: str | None, top_10_domains: list[str],
) -> None:
    cur.execute(
        """
        INSERT INTO keyword_serp_snapshots
            (keyword_id, date, location_code, device, serp_features,
             ai_overview_present, ai_overview_citations, total_results_seen,
             damco_position, damco_url, top_10_domains)
        VALUES (%s, %s, %s, 'desktop', %s::jsonb,
                %s, %s::jsonb, %s,
                %s, %s, %s::jsonb)
        ON CONFLICT (keyword_id, date, device) DO UPDATE SET
            serp_features         = EXCLUDED.serp_features,
            ai_overview_present   = EXCLUDED.ai_overview_present,
            ai_overview_citations = EXCLUDED.ai_overview_citations,
            total_results_seen    = EXCLUDED.total_results_seen,
            damco_position        = EXCLUDED.damco_position,
            damco_url             = EXCLUDED.damco_url,
            top_10_domains        = EXCLUDED.top_10_domains
        """,
        (
            keyword_id, run_date, settings.DATAFORSEO_LOCATION_CODE,
            json.dumps(serp_features), ai_present, json.dumps(ai_citations),
            total_results, damco_position, damco_url, json.dumps(top_10_domains),
        ),
    )


def write_competitor_rankings(
    cur, *, keyword_id: int, run_date: date, current_top: list[dict],
    prev_pos_by_domain: dict[str, int],
) -> set[int]:
    """
    Insert one row per top-N competitor for this snapshot.
    Returns the set of touched competitor_ids (for aggregate recompute).
    """
    touched: set[int] = set()
    for item in current_top:
        domain = item["domain"]
        cid = upsert_competitor(cur, domain, run_date)
        touched.add(cid)

        prev_pos = prev_pos_by_domain.get(domain)
        position_change = None
        is_new = prev_pos is None
        if not is_new:
            position_change = item["rank_position"] - prev_pos

        cur.execute(
            """
            INSERT INTO competitor_rankings
                (competitor_id, keyword_id, date, rank_position, url_found,
                 url_title, page_type, serp_features_owned,
                 is_new_entrant, previous_position, position_change)
            VALUES (%s, %s, %s, %s, %s,
                    %s, %s, %s::jsonb,
                    %s, %s, %s)
            ON CONFLICT (competitor_id, keyword_id, date) DO UPDATE SET
                rank_position       = EXCLUDED.rank_position,
                url_found           = EXCLUDED.url_found,
                url_title           = EXCLUDED.url_title,
                page_type           = EXCLUDED.page_type,
                serp_features_owned = EXCLUDED.serp_features_owned,
                is_new_entrant      = EXCLUDED.is_new_entrant,
                previous_position   = EXCLUDED.previous_position,
                position_change     = EXCLUDED.position_change
            """,
            (
                cid, keyword_id, run_date, item["rank_position"], item["url"],
                item["title"], item["page_type"], json.dumps(item["features_owned"]),
                is_new, prev_pos, position_change,
            ),
        )
    return touched


def write_events(cur, events: list[dict], run_date: date,
                 keyword_id: int | None, domain_to_cid: dict[str, int]) -> int:
    """Insert event rows. Returns count written."""
    written = 0
    for ev in events:
        cid = domain_to_cid.get(ev.get("domain")) if ev.get("domain") else None
        cur.execute(
            """
            INSERT INTO competitor_serp_events
                (event_type, keyword_id, competitor_id, event_date,
                 old_value, new_value, delta, severity, metadata)
            VALUES (%s, %s, %s, %s,
                    %s::jsonb, %s::jsonb, %s, %s,
                    %s::jsonb)
            """,
            (
                ev["event_type"], keyword_id, cid, run_date,
                json.dumps(ev["old_value"]), json.dumps(ev["new_value"]),
                ev.get("delta"), ev["severity"],
                json.dumps(ev.get("metadata", {})),
            ),
        )
        written += 1
    return written


def store_keyword_rankings(cur, keyword_id: int, run_date: date,
                           rank_position: int | None, url_found: str | None) -> None:
    """Damco-side row in keyword_rankings (existing behavior, unchanged)."""
    cur.execute(
        """
        INSERT INTO keyword_rankings
            (keyword_id, date, rank_position, rank_bucket, url_found, source)
        VALUES (%s, %s, %s, %s, %s, 'dataforseo')
        ON CONFLICT (keyword_id, date, source) DO UPDATE SET
            rank_position = EXCLUDED.rank_position,
            rank_bucket   = EXCLUDED.rank_bucket,
            url_found     = EXCLUDED.url_found
        """,
        (keyword_id, run_date, rank_position, rank_bucket(rank_position), url_found),
    )


# ---------------------------------------------------------------------------
# Orchestration: per-keyword write
# ---------------------------------------------------------------------------

def process_keyword(cur, *, keyword_id: int, raw: dict | None,
                    items: list[dict], run_date: date) -> dict:
    """Do everything for one keyword: extract, diff, write. Returns counters."""
    # 1. Extract
    serp_features, ai_present, ai_citations = extract_serp_features(raw)
    organic_items = [it for it in items if it.get("type") == "organic"]
    top_n = organic_items[:TOP_N]

    # Enrich each top-N item with page_type and per-URL feature ownership
    current_top: list[dict] = []
    for it in top_n:
        domain = (it.get("domain") or "").lower()
        page_type = categorize_page_type(it.get("url"), it.get("title"), domain)
        features_owned: list[str] = []
        # AI Overview citation ownership
        if any(c.get("domain", "").lower() == domain for c in ai_citations):
            features_owned.append("ai_overview_cited")
        current_top.append({
            "domain":          domain,
            "rank_position":   it.get("rank_group"),
            "url":             it.get("url"),
            "title":           it.get("title"),
            "page_type":       page_type,
            "features_owned":  features_owned,
        })

    # Damco position (search the full SERP, not just top N)
    damco_hit = find_brand_position(organic_items)
    damco_pos = damco_hit["rank_group"] if damco_hit else None
    damco_url = damco_hit.get("url") if damco_hit else None

    # 2. Load previous snapshot for diff
    prev = load_previous_snapshot(cur, keyword_id)
    prev_top = prev["top_10"] if prev else []
    prev_pos_by_domain = {x["domain"]: x["rank_position"] for x in prev_top}
    prev_features = prev["serp_features"] if prev else []
    prev_damco_pos = prev["damco_position"] if prev else None

    # 3. Compute diffs
    competitor_events = diff_top_n(prev_top, current_top) if prev else []
    damco_events      = diff_damco(prev_damco_pos, damco_pos) if prev else []
    feature_events    = diff_serp_features(prev_features, serp_features) if prev else []
    all_events = competitor_events + damco_events + feature_events

    # 4. Write Damco-side row (existing keyword_rankings)
    store_keyword_rankings(cur, keyword_id, run_date, damco_pos, damco_url)

    # 5. Write keyword_serp_snapshots
    write_keyword_serp_snapshot(
        cur,
        keyword_id=keyword_id, run_date=run_date,
        serp_features=serp_features, ai_present=ai_present, ai_citations=ai_citations,
        total_results=len(organic_items),
        damco_position=damco_pos, damco_url=damco_url,
        top_10_domains=[x["domain"] for x in current_top],
    )

    # 6. Write competitor_rankings (and stub competitors as needed)
    touched_cids = write_competitor_rankings(
        cur, keyword_id=keyword_id, run_date=run_date,
        current_top=current_top, prev_pos_by_domain=prev_pos_by_domain,
    )

    # Build domain → cid map for event writes
    cur.execute(
        "SELECT id, competitor_domain FROM competitors WHERE id = ANY(%s)",
        (list(touched_cids),),
    )
    domain_to_cid = {row[1] if not isinstance(row, dict) else row["competitor_domain"]:
                     row[0] if not isinstance(row, dict) else row["id"]
                     for row in cur.fetchall()}

    # 7. Write events
    events_written = write_events(cur, all_events, run_date, keyword_id, domain_to_cid)

    return {
        "damco_position":   damco_pos,
        "damco_url":        damco_url,
        "top_n_count":      len(current_top),
        "events_written":   events_written,
        "touched_cids":     touched_cids,
        "ai_present":       ai_present,
    }


def recompute_aggregates(competitor_ids: set[int]) -> None:
    """Call recompute_competitor_aggregates() for each touched competitor."""
    if not competitor_ids:
        return
    with connection() as conn:
        with conn.cursor() as cur:
            for cid in competitor_ids:
                cur.execute("SELECT recompute_competitor_aggregates(%s)", (cid,))


def refresh_offering_rollup() -> None:
    """REFRESH MATERIALIZED VIEW CONCURRENTLY (needs autocommit)."""
    with connection() as conn:
        prior_autocommit = conn.autocommit
        try:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute("REFRESH MATERIALIZED VIEW CONCURRENTLY mv_offering_competition")
        finally:
            conn.autocommit = prior_autocommit


# ---------------------------------------------------------------------------
# Console summary
# ---------------------------------------------------------------------------

def print_summary(results: list[dict], run_date: date) -> None:
    print()
    print(f"  {'=' * 72}")
    print(f"   DAMCO Rank Tracker — {run_date.isoformat()}")
    print(f"  {'=' * 72}")
    print()

    buckets: dict[str, int] = {}
    for r in results:
        b = rank_bucket(r["rank_position"])
        buckets[b] = buckets.get(b, 0) + 1

    print("  Bucket Distribution:")
    for bucket_name in ["1-5", "5-10", "10-20", "20-50", "50+", "not-found"]:
        count = buckets.get(bucket_name, 0)
        bar = "#" * count
        print(f"    {bucket_name:>10}  {count:>3}  {bar}")
    print()

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
        skip_gsc: bool = False, gsc_days: int = 14, force_all: bool = False) -> dict:
    start_time = time.monotonic()
    run_date = date.today()

    keywords = load_keywords(offering, only_due=not force_all)
    if not keywords:
        msg = "No keywords are due for a snapshot"
        if offering: msg += f" (offering={offering})"
        if force_all: msg = "No active keywords found"
        logger.warning(msg)
        return {"status": "skipped", "reason": "no keywords"}

    logger.info("Tracking %d keywords (queue=%s, date=%s, force_all=%s)",
                len(keywords), queue, run_date, force_all)

    serp_data = fetch_rankings(keywords, queue)

    # Per-keyword writes
    results: list[dict] = []
    api_errors: list[str] = []
    total_events = 0
    total_touched_cids: set[int] = set()
    snapshots_written = 0

    if dry_run:
        logger.info("DRY RUN — skipping all writes")
        for kw in keywords:
            d = serp_data.get(kw["id"], {})
            organic = [it for it in (d.get("items") or []) if it.get("type") == "organic"]
            damco = find_brand_position(organic)
            results.append({
                "keyword":       kw["keyword"],
                "rank_position": damco["rank_group"] if damco else None,
                "url_found":     damco.get("url") if damco else None,
                "error":         d.get("error"),
            })
            if d.get("error"):
                api_errors.append(d["error"])
    else:
        with connection() as conn:
            cur = conn.cursor()
            for kw in keywords:
                d = serp_data.get(kw["id"])
                if d is None or d.get("error"):
                    err = d["error"] if d else "no DataForSEO result"
                    api_errors.append(err)
                    results.append({
                        "keyword":       kw["keyword"],
                        "rank_position": None,
                        "url_found":     None,
                        "error":         err,
                    })
                    continue
                try:
                    pk = process_keyword(
                        cur,
                        keyword_id=kw["id"],
                        raw=d.get("raw"),
                        items=d.get("items") or [],
                        run_date=run_date,
                    )
                    conn.commit()
                    snapshots_written += 1
                    total_events       += pk["events_written"]
                    total_touched_cids |= pk["touched_cids"]
                    results.append({
                        "keyword":       kw["keyword"],
                        "rank_position": pk["damco_position"],
                        "url_found":     pk["damco_url"],
                        "error":         None,
                    })
                except Exception as exc:
                    conn.rollback()
                    logger.error("Failed to process keyword_id=%s: %s", kw["id"], exc)
                    api_errors.append(f"{kw['keyword']}: {exc}")
                    results.append({
                        "keyword":       kw["keyword"],
                        "rank_position": None,
                        "url_found":     None,
                        "error":         str(exc),
                    })

        # Aggregate recomputation + view refresh after the main loop
        try:
            recompute_aggregates(total_touched_cids)
        except Exception as exc:
            logger.error("recompute_aggregates failed: %s", exc)
            api_errors.append(f"aggregates: {exc}")
        try:
            refresh_offering_rollup()
        except Exception as exc:
            logger.error("refresh_offering_rollup failed: %s", exc)
            api_errors.append(f"refresh_view: {exc}")

    duration = time.monotonic() - start_time
    total = len(results)
    found = sum(1 for r in results if r["rank_position"] is not None)
    err_count = sum(1 for r in results if r.get("error"))

    if not dry_run:
        record_agent_run(
            agent_name=AGENT_NAME,
            status="success" if err_count == 0 else "partial",
            records_processed=snapshots_written,
            errors=api_errors,
            duration_seconds=round(duration, 2),
            metadata={
                "run_date":            run_date.isoformat(),
                "queue":               queue,
                "offering_filter":     offering,
                "force_all":           force_all,
                "total_keywords":      total,
                "found":               found,
                "not_found":           total - found - err_count,
                "errors":              err_count,
                "events_emitted":      total_events,
                "competitors_touched": len(total_touched_cids),
            },
        )

    print_summary(results, run_date)
    print(f"  Keywords tracked:     {total}")
    print(f"  Damco found:          {found}")
    print(f"  Not found:            {total - found - err_count}")
    print(f"  Errors:               {err_count}")
    print(f"  SERP snapshots:       {snapshots_written}")
    print(f"  Events emitted:       {total_events}")
    print(f"  Competitors touched:  {len(total_touched_cids)}")
    print(f"  Duration:             {duration:.1f}s")
    print(f"  Estimated cost:       ~${total * 0.0006:.4f} (standard queue)")
    print()

    # GSC enrichment
    gsc_stats: dict | None = None
    if not dry_run and not skip_gsc:
        try:
            from keyword_intelligence.gsc_enrichment import run as gsc_run
            gsc_stats = gsc_run(lookback_days=gsc_days, dry_run=dry_run)
        except Exception as exc:
            logger.warning("GSC enrichment failed (non-fatal): %s", exc)
            gsc_stats = {"status": "error", "error": str(exc)}

    return {
        "status":              "success" if err_count == 0 else "partial",
        "run_date":            run_date.isoformat(),
        "total":               total,
        "found":               found,
        "errors":              err_count,
        "snapshots_written":   snapshots_written,
        "events_emitted":      total_events,
        "competitors_touched": len(total_touched_cids),
        "duration_seconds":    round(duration, 2),
        "gsc":                 gsc_stats,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Damco Keyword Rank Tracker")
    parser.add_argument("--offering", help="Track only keywords for a specific offering")
    parser.add_argument("--queue", default="standard", choices=["standard", "live"],
                        help="DataForSEO queue tier (default: standard)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Fetch rankings but don't write to DB")
    parser.add_argument("--all", dest="force_all", action="store_true",
                        help="Force a snapshot for every active keyword (ignores fortnightly cadence)")
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
        skip_gsc=args.skip_gsc, gsc_days=args.gsc_days, force_all=args.force_all)


if __name__ == "__main__":
    main()
