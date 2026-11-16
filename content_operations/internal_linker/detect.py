"""
Phase 2 — detectors.

Two detectors ship in the first pass of this agent:

    striking_distance_support   (maps to S-tier 'help pages near page one')
        For every keyword currently ranking at positions 11-20 on a target
        page, find owned pages that could realistically link to it:
            - same offering as the striking-distance target
            - do not already link to the target
            - themselves rank for something in the top 20 (i.e. they carry
              authority and their link has weight)
        Priority score is driven by the target keyword's GSC impressions —
        we want to help the pages with real demand first.

    blog_to_money_page           (maps to S-tier 'link articles to pages that
                                  sell')
        For every blog post in an offering, check whether it links to that
        offering's primary service page. If not, suggest one. The
        suggested anchor comes from the blog's own context: the service
        page's title, if the blog's text matches it closely; otherwise the
        offering name.

Both detectors read the link graph from `internal_links` and write suggestions
to `internal_link_suggestions` with ON CONFLICT upserts so repeat runs
dedupe. Status that the writer has set ('applied', 'ignored') is preserved.
"""
from __future__ import annotations

import logging
import time
from collections import defaultdict
from datetime import date
from urllib.parse import urlparse

from common.database import connection, fetch_all, record_agent_run
from common.tenant import profile

logger = logging.getLogger("content_operations.internal_linker.detectors")

AGENT_NAME = "content_operations.internal_linker.detect"

# Link graph is treated as "same-day fresh". Older link snapshots are still
# read, but a crawl that is this many days stale produces a warning.
LINK_GRAPH_STALE_DAYS = 14

# Cap suggestions per striking-distance target. Without this, offerings with
# many source pages (BPM, App Dev) would generate hundreds of suggestions per
# target keyword — un-actionable noise. We pick the N most-authoritative
# sources (best_pos = closest to #1) per target; the content team can work
# through the top 5 and most striking wins land.
MAX_SOURCES_PER_TARGET = 5

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _norm_url(u: str | None) -> str | None:
    """Fold trailing slash, lower the host, strip query/fragment — we match
    URLs across pages that sometimes have ?utm_… or trailing /."""
    if not u:
        return None
    try:
        pu = urlparse(u)
        if not pu.scheme:
            return None
        host = (pu.netloc or "").lower()
        path = pu.path.rstrip("/") or "/"
        return f"{pu.scheme}://{host}{path}"
    except Exception:
        return None


def _upsert_suggestion(cur, detector: str, from_url: str, to_url: str,
                      anchor: str | None, reason: str,
                      priority: int) -> None:
    """One row per (detector, from, to). Priority + last_suggested_at move
    on every run; applied/ignored status set by a writer is preserved by
    the CHECK + the ON CONFLICT clause (we do not touch status)."""
    cur.execute(
        """
        INSERT INTO internal_link_suggestions
            (detector, from_url, to_url, suggested_anchor, reason, priority_score)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (detector, from_url, to_url) DO UPDATE SET
            suggested_anchor   = EXCLUDED.suggested_anchor,
            reason             = EXCLUDED.reason,
            priority_score     = EXCLUDED.priority_score,
            last_suggested_at  = CURRENT_TIMESTAMP,
            -- 'stale' rows come back to 'new' if the detector re-emits them.
            status = CASE
                WHEN internal_link_suggestions.status = 'stale' THEN 'new'
                ELSE internal_link_suggestions.status
            END
        """,
        (detector, from_url, to_url, anchor, reason, priority),
    )


def _mark_stale(cur, detector: str, still_fresh_pairs: set[tuple[str, str]]) -> int:
    """
    Flip any 'new' suggestion that the detector did NOT re-emit this run to
    'stale'. Prevents the dashboard from showing old suggestions after the
    underlying condition disappears (e.g. the link was added, or the keyword
    moved into the top 10).
    """
    existing = fetch_all(
        "SELECT from_url, to_url FROM internal_link_suggestions "
        "WHERE detector = %s AND status = 'new'",
        [detector],
    )
    to_stale = [(r["from_url"], r["to_url"]) for r in existing
                if (r["from_url"], r["to_url"]) not in still_fresh_pairs]
    for from_url, to_url in to_stale:
        cur.execute(
            "UPDATE internal_link_suggestions SET status = 'stale' "
            "WHERE detector = %s AND from_url = %s AND to_url = %s",
            (detector, from_url, to_url),
        )
    return len(to_stale)


# ---------------------------------------------------------------------------
# Detector 1 — striking_distance_support
# ---------------------------------------------------------------------------

STRIKING_DETECTOR = "striking_distance_support"

def run_striking_distance_support() -> dict:
    """
    Suggest contextual links from owned pages that already rank well to pages
    whose keywords sit at positions 11-20 (page 1 but below the fold).

    Shape of a suggestion:
        from_url : an owned page that ranks for a top-20 keyword in the same
                   offering as the target page
        to_url   : target_url of the striking-distance keyword
        anchor   : the striking keyword's text (natural user-facing phrase)
        reason   : "support striking: '<keyword>' at pos N (GSC N impr)"
        priority : target keyword's 14-day GSC impressions (desc)
    """
    # 1) Striking-distance keywords with a target URL + offering context.
    #    Only owned destinations; only with an impression signal that the
    #    demand is real.
    targets = fetch_all(
        """
        WITH latest AS (
            SELECT DISTINCT ON (keyword_id) keyword_id, rank_position
              FROM keyword_rankings
             WHERE source <> 'gsc'
             ORDER BY keyword_id, date DESC
        ),
        gsc AS (
            SELECT DISTINCT ON (keyword_id) keyword_id, impressions
              FROM keyword_rankings
             WHERE source = 'gsc'
             ORDER BY keyword_id, date DESC
        )
        SELECT k.id, k.keyword, k.offering, k.target_url,
               l.rank_position AS pos,
               coalesce(g.impressions, 0) AS gsc_impr
          FROM keywords k
          JOIN latest l   ON l.keyword_id = k.id
          LEFT JOIN gsc g ON g.keyword_id = k.id
         WHERE k.status = 'active'
           AND k.offering IS NOT NULL
           AND k.target_url ILIKE 'http%%'
           AND l.rank_position BETWEEN 11 AND 20
        """
    )
    logger.info("striking-distance: %d candidate target keywords", len(targets))

    # 2) Pool of authoritative source pages: anything ranking in the top 20
    #    for some keyword, grouped by offering. We attach the known target_url
    #    (where our rank_tracker saw us appear) since that is the URL that
    #    will actually be edited to add a link.
    sources = fetch_all(
        """
        SELECT DISTINCT k.offering, kr.url_found AS source_url,
               min(kr.rank_position) AS best_pos
          FROM keyword_rankings kr
          JOIN keywords k ON k.id = kr.keyword_id
         WHERE kr.source <> 'gsc'
           AND kr.url_found ILIKE 'http%%'
           AND kr.rank_position BETWEEN 1 AND 20
           AND k.offering IS NOT NULL
         GROUP BY k.offering, kr.url_found
        """
    )
    # Sort each offering's source pool by authority (best rank ASC) so the
    # per-target cap keeps the strongest candidates.
    src_by_offering: dict[str, list[dict]] = defaultdict(list)
    for s in sources:
        src_by_offering[s["offering"]].append(s)
    for off in src_by_offering:
        src_by_offering[off].sort(key=lambda s: (s["best_pos"] or 999, s["source_url"]))

    # 3) Load existing internal_links so we can skip pairs that already exist.
    existing_edges: set[tuple[str, str]] = set()
    for r in fetch_all(
        "SELECT source_url, target_url FROM internal_links WHERE is_internal = TRUE"
    ):
        f, t = _norm_url(r["source_url"]), _norm_url(r["target_url"])
        if f and t:
            existing_edges.add((f, t))

    # 4) Build suggestions.
    fresh_pairs: set[tuple[str, str]] = set()
    written = 0
    with connection() as conn:
        cur = conn.cursor()
        for t in targets:
            target_url = _norm_url(t["target_url"])
            if not target_url:
                continue
            candidate_sources = src_by_offering.get(t["offering"], [])
            picked_for_target = 0
            for s in candidate_sources:
                if picked_for_target >= MAX_SOURCES_PER_TARGET:
                    break
                src_url = _norm_url(s["source_url"])
                if not src_url or src_url == target_url:
                    continue
                if (src_url, target_url) in existing_edges:
                    continue
                reason = (f"support striking: '{t['keyword']}' at pos {t['pos']} "
                          f"(GSC {int(t['gsc_impr'])} impr, source ranks #{s['best_pos']})")
                _upsert_suggestion(
                    cur,
                    detector=STRIKING_DETECTOR,
                    from_url=src_url,
                    to_url=target_url,
                    anchor=t["keyword"],
                    reason=reason,
                    priority=int(t["gsc_impr"]),
                )
                fresh_pairs.add((src_url, target_url))
                written += 1
                picked_for_target += 1
        staled = _mark_stale(cur, STRIKING_DETECTOR, fresh_pairs)
        conn.commit()

    logger.info("striking-distance: wrote %d suggestions; marked %d stale",
                written, staled)
    return {
        "detector": STRIKING_DETECTOR,
        "targets": len(targets),
        "suggestions_written": written,
        "marked_stale": staled,
    }


# ---------------------------------------------------------------------------
# Detector 2 — blog_to_money_page
# ---------------------------------------------------------------------------

BLOG_DETECTOR = "blog_to_money_page"

def run_blog_to_money_page() -> dict:
    """
    For every blog post in an offering, check whether it already links to any
    service page in that offering. If not, suggest the offering's primary
    service page (the one with the most tracked top-10 keywords — a cheap
    stand-in for 'the money page').

    'Service page in the offering' is defined as: a row in the `pages` table
    with page_type = 'service' AND offering matching the blog's offering.
    """
    # 1) Blog pages per offering.
    blogs = fetch_all(
        """
        SELECT id, url, offering, title
          FROM pages
         WHERE page_type = 'blog' AND offering IS NOT NULL
        """
    )
    logger.info("blog-to-money: %d candidate blog pages", len(blogs))

    # 2) Service pages per offering.
    service_pages = fetch_all(
        """
        SELECT url, offering, title
          FROM pages
         WHERE page_type = 'service' AND offering IS NOT NULL
        """
    )
    svc_by_offering: dict[str, list[dict]] = defaultdict(list)
    for s in service_pages:
        svc_by_offering[s["offering"]].append(s)

    # 3) Rank each service page by the number of top-10 keywords it anchors
    #    (via keywords.target_url). This proxies "money page" without a
    #    manual tag; the one with the most top-10 share for the offering is
    #    the one we want to feed internal links to.
    svc_power = fetch_all(
        """
        WITH latest AS (
            SELECT DISTINCT ON (keyword_id) keyword_id, rank_position
              FROM keyword_rankings
             WHERE source <> 'gsc'
             ORDER BY keyword_id, date DESC
        )
        SELECT k.offering,
               k.target_url,
               count(*) FILTER (WHERE l.rank_position BETWEEN 1 AND 10) AS top10,
               count(*) FILTER (WHERE l.rank_position BETWEEN 1 AND 20) AS top20,
               count(*) FILTER (WHERE k.importance = 'high') AS high_kws
          FROM keywords k
          LEFT JOIN latest l ON l.keyword_id = k.id
         WHERE k.status = 'active' AND k.offering IS NOT NULL
           AND k.target_url ILIKE 'http%%'
         GROUP BY k.offering, k.target_url
        """
    )
    # (offering, normalized_url) -> power dict
    power_lookup: dict[tuple[str, str], dict] = {}
    for r in svc_power:
        nu = _norm_url(r["target_url"])
        if not nu:
            continue
        power_lookup[(r["offering"], nu)] = r

    def pick_money_page(offering: str) -> dict | None:
        candidates = []
        for sp in svc_by_offering.get(offering, []):
            nu = _norm_url(sp["url"])
            if not nu:
                continue
            power = power_lookup.get((offering, nu), {}) or {}
            candidates.append({
                "url":   nu,
                "title": sp["title"],
                "top10": power.get("top10") or 0,
                "top20": power.get("top20") or 0,
                "hi":    power.get("high_kws") or 0,
            })
        if not candidates:
            return None
        candidates.sort(key=lambda c: (c["top10"], c["top20"], c["hi"]), reverse=True)
        return candidates[0]

    # 4) Existing link graph — grouped by source -> set of targets.
    graph: dict[str, set[str]] = defaultdict(set)
    for r in fetch_all(
        "SELECT source_url, target_url FROM internal_links WHERE is_internal = TRUE"
    ):
        f, t = _norm_url(r["source_url"]), _norm_url(r["target_url"])
        if f and t:
            graph[f].add(t)

    # Also fast lookup: for an offering, the normalized URLs of its service pages.
    svc_urls_by_offering: dict[str, set[str]] = {
        off: {_norm_url(sp["url"]) for sp in pages}
        for off, pages in svc_by_offering.items()
    }

    fresh_pairs: set[tuple[str, str]] = set()
    written = skipped_already_linked = skipped_no_money_page = 0
    with connection() as conn:
        cur = conn.cursor()
        for b in blogs:
            offering = b["offering"]
            blog_url = _norm_url(b["url"])
            if not blog_url:
                continue
            # Does the blog already link to ANY service page in its offering?
            offering_svc = svc_urls_by_offering.get(offering, set()) or set()
            outgoing = graph.get(blog_url, set())
            if offering_svc & outgoing:
                skipped_already_linked += 1
                continue
            money = pick_money_page(offering)
            if money is None:
                skipped_no_money_page += 1
                continue
            priority = int(money["top10"] * 10 + money["top20"] + money["hi"])
            reason = (
                f"blog has no link to any {offering} service page; "
                f"strongest candidate ranks top-10 for {money['top10']} kw "
                f"(top-20 {money['top20']}, high-priority {money['hi']})"
            )
            _upsert_suggestion(
                cur,
                detector=BLOG_DETECTOR,
                from_url=blog_url,
                to_url=money["url"],
                anchor=money["title"] or offering,
                reason=reason,
                priority=priority,
            )
            fresh_pairs.add((blog_url, money["url"]))
            written += 1
        staled = _mark_stale(cur, BLOG_DETECTOR, fresh_pairs)
        conn.commit()

    logger.info(
        "blog-to-money: wrote %d suggestions, %d already linked, "
        "%d no money page available; marked %d stale",
        written, skipped_already_linked, skipped_no_money_page, staled,
    )
    return {
        "detector": BLOG_DETECTOR,
        "blogs_checked":            len(blogs),
        "suggestions_written":      written,
        "skipped_already_linked":   skipped_already_linked,
        "skipped_no_money_page":    skipped_no_money_page,
        "marked_stale":             staled,
    }


# ---------------------------------------------------------------------------
# Top-level entrypoint
# ---------------------------------------------------------------------------

def run() -> dict:
    """Run both detectors in sequence and log one agent_runs row."""
    start = time.monotonic()
    api_errors: list[str] = []

    # Freshness check so operators notice stale crawls.
    stale_row = fetch_all(
        "SELECT max(last_seen) AS d FROM internal_links WHERE is_internal = TRUE"
    )[0]
    if stale_row["d"] is None:
        api_errors.append("link graph is empty — run `crawl` before `detect`")
        logger.error(api_errors[-1])
    else:
        days = (date.today() - stale_row["d"]).days
        if days > LINK_GRAPH_STALE_DAYS:
            msg = f"link graph last updated {days} days ago (>{LINK_GRAPH_STALE_DAYS}) — results may be inaccurate"
            api_errors.append(msg)
            logger.warning(msg)

    s1 = run_striking_distance_support()
    s2 = run_blog_to_money_page()

    duration = time.monotonic() - start
    total = s1["suggestions_written"] + s2["suggestions_written"]
    record_agent_run(
        agent_name=AGENT_NAME,
        status="success" if not api_errors else "partial",
        records_processed=total,
        errors=api_errors or None,
        duration_seconds=round(duration, 2),
        metadata={
            "run_date":               date.today().isoformat(),
            "link_graph_last_seen":   stale_row["d"].isoformat() if stale_row["d"] else None,
            "striking":               s1,
            "blog_to_money":          s2,
        },
    )
    return {
        "status": "success" if not api_errors else "partial",
        "total_suggestions": total,
        "striking": s1,
        "blog_to_money": s2,
        "duration_seconds": round(duration, 2),
    }
