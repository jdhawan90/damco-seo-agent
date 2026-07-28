"""
Dashboard KPIs — one function per tile, plain SQL, no LLM.
==========================================================

Every function returns a dict carrying its own `as_of` and `stale_days`.

Why freshness is part of the contract
------------------------------------
These agents run on wildly different cadences and, until the schedule landed,
several had not run in months. A tile reading "130 open technical issues" is
worse than no tile if the number is eleven weeks old and looks current. So
staleness is not a footnote here — it travels with the value, derived from the
data itself rather than from page-load time.

A note on measurement cycles
----------------------------
Not every `keyword_rankings` date is a full run. One date holds 2,126 keywords,
another 1,176, another 123. Comparing "latest date vs previous date" would
report ~950 keywords as having moved when they simply weren't measured. So
movement is computed **per keyword, between its own two most recent
measurements**, whatever dates those fall on — and the average span is reported
so a six-week gap can't masquerade as a fortnight's drift.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any

from common.database import fetch_all, fetch_one
from common.tenant import profile


logger = logging.getLogger(__name__)

# Ordinal ramp for the position distribution, validated with
# scripts/validate_palette.js --ordinal in both modes (all checks pass).
# Best position is the darkest step on light, the brightest on dark.
# "not-found" is deliberately NOT on the ramp — absence of a position is not a
# weaker position, and colouring it blue implies a rank it does not have.
BUCKET_ORDER = ("1-5", "5-10", "10-20", "20-50", "50+", "not-found")

# A date is treated as a measurement cycle only if it covers a meaningful
# share of the tracked set. Without this the trendline shows a cliff every
# time someone ran the tracker against a single offering.
MIN_CYCLE_SAMPLE = 300

# Dates this far apart or closer belong to the same tracker run. A full run over
# 2,000 keywords takes hours and routinely crosses midnight, landing two dates in
# the table with disjoint keyword sets — which plots as a dip and a recovery that
# never happened.
CYCLE_MERGE_DAYS = 1


def _stale(as_of: Any) -> dict:
    """Normalize a timestamp into the freshness contract every tile carries."""
    if as_of is None:
        return {"as_of": None, "stale_days": None}
    d = as_of.date() if isinstance(as_of, datetime) else as_of
    return {"as_of": d.isoformat(), "stale_days": (date.today() - d).days}


# ---------------------------------------------------------------------------
# Tier 1 — executive
# ---------------------------------------------------------------------------

def visibility() -> dict:
    """
    Hero figure: share of tracked keywords in the top 10, plus its trend.

    Exactly one hero number per view, per the marks spec — this is it.
    """
    latest = fetch_one(
        "SELECT max(date) AS d FROM keyword_rankings WHERE source <> 'gsc'"
    )
    latest_date = latest["d"] if latest else None
    if not latest_date:
        return {"value": None, "trend": [], **_stale(None)}

    cur = fetch_one(
        """
        SELECT count(*) AS measured,
               count(*) FILTER (WHERE rank_position IS NOT NULL
                                  AND rank_position <= 10) AS in_top_10
          FROM keyword_rankings
         WHERE source <> 'gsc' AND date = %s
        """,
        [latest_date],
    )
    pct = round(100.0 * cur["in_top_10"] / cur["measured"], 1) if cur["measured"] else 0.0

    trend = _visibility_trend()
    prev = trend[-2] if len(trend) >= 2 else None

    # The delta is computed on the keywords measured in BOTH of the last two
    # cycles, not by subtracting two percentages over different denominators.
    #
    # That distinction is load-bearing. Raw per-cycle shares here run 12.8% on
    # 989 keywords, 23.9% on 1,109, then 19.1% on 2,126 — which reads as a
    # 4.8-point collapse in July. Nothing collapsed; the tracked set nearly
    # doubled. Publishing that subtraction would have someone investigating a
    # decline that never happened.
    comparable = None
    if prev:
        comparable = fetch_one(
            """
            WITH shared AS (
                SELECT keyword_id
                  FROM keyword_rankings
                 WHERE source <> 'gsc'
                   AND (date = ANY(%s) OR date = ANY(%s))
                 GROUP BY keyword_id
                HAVING count(DISTINCT CASE WHEN date = ANY(%s) THEN 1 ELSE 2 END) = 2
            )
            SELECT count(*) FILTER (WHERE date = ANY(%s)) AS n_now,
                   round(100.0 * count(*) FILTER (
                       WHERE date = ANY(%s) AND rank_position <= 10)
                       / NULLIF(count(*) FILTER (WHERE date = ANY(%s)), 0), 1) AS pct_now,
                   round(100.0 * count(*) FILTER (
                       WHERE date = ANY(%s) AND rank_position <= 10)
                       / NULLIF(count(*) FILTER (WHERE date = ANY(%s)), 0), 1) AS pct_prev
              FROM keyword_rankings
             WHERE source <> 'gsc'
               AND keyword_id IN (SELECT keyword_id FROM shared)
               AND (date = ANY(%s) OR date = ANY(%s))
            """,
            [trend[-1]["dates"], prev["dates"], trend[-1]["dates"],
             trend[-1]["dates"], trend[-1]["dates"], trend[-1]["dates"],
             prev["dates"], prev["dates"],
             trend[-1]["dates"], prev["dates"]],
        )

    delta = None
    delta_basis = None
    if comparable and comparable["pct_now"] is not None and comparable["pct_prev"] is not None:
        delta = round(float(comparable["pct_now"]) - float(comparable["pct_prev"]), 1)
        delta_basis = (
            f"on the {comparable['n_now']} keywords measured in both cycles "
            f"({float(comparable['pct_prev'])}% -> {float(comparable['pct_now'])}%)"
        )

    # `dates` carries real date objects, needed above to build the shared-cohort
    # query. Strip it before returning: the client only needs the label and the
    # span count, and raw dates are not JSON-serializable by FastAPI's encoder.
    public_trend = [{k: v for k, v in t.items() if k != "dates"} for t in trend]

    return {
        "value": pct,
        "in_top_10": cur["in_top_10"],
        "measured": cur["measured"],
        "delta": delta,
        "delta_vs": prev["label"] if prev else None,
        "delta_basis": delta_basis,
        "trend": public_trend,
        **_stale(latest_date),
    }


def _visibility_trend() -> list[dict]:
    """
    Per-cycle top-10 share, with runs that straddle midnight merged.

    A "cycle" is not a calendar date. 2026-06-30 (846 keywords) and 2026-07-01
    (1,176) share ZERO keywords — they are one tracker run that crossed
    midnight. Plotted as two points they produce a fake dip and a fake
    recovery. Dates within `CYCLE_MERGE_DAYS` of each other are therefore
    collapsed into one cycle and aggregated.
    """
    dates = [
        r["date"]
        for r in fetch_all(
            """
            SELECT date FROM keyword_rankings
             WHERE source <> 'gsc'
             GROUP BY date HAVING count(*) >= %s
             ORDER BY date
            """,
            [MIN_CYCLE_SAMPLE],
        )
    ]
    if not dates:
        return []

    # Group adjacent dates into cycles.
    groups: list[list] = [[dates[0]]]
    for d in dates[1:]:
        if (d - groups[-1][-1]).days <= CYCLE_MERGE_DAYS:
            groups[-1].append(d)
        else:
            groups.append([d])

    out = []
    for g in groups:
        row = fetch_one(
            """
            SELECT count(*) AS measured,
                   count(*) FILTER (WHERE rank_position IS NOT NULL
                                      AND rank_position <= 10) AS in_top_10
              FROM keyword_rankings
             WHERE source <> 'gsc' AND date = ANY(%s)
            """,
            [g],
        )
        out.append({
            "label": g[-1].isoformat(),
            "dates": g,
            "spans_dates": len(g),
            "measured": row["measured"],
            "in_top_10": row["in_top_10"],
            "pct": round(100.0 * row["in_top_10"] / row["measured"], 1)
                   if row["measured"] else 0.0,
        })
    return out


def position_distribution() -> dict:
    """Bucket counts on the latest cycle. Ordered categories -> ordinal ramp."""
    latest = fetch_one(
        "SELECT max(date) AS d FROM keyword_rankings WHERE source <> 'gsc'"
    )
    latest_date = latest["d"] if latest else None
    if not latest_date:
        return {"buckets": [], **_stale(None)}

    rows = {
        r["rank_bucket"]: r["n"]
        for r in fetch_all(
            """
            SELECT COALESCE(rank_bucket, 'not-found') AS rank_bucket, count(*) AS n
              FROM keyword_rankings
             WHERE source <> 'gsc' AND date = %s
             GROUP BY 1
            """,
            [latest_date],
        )
    }
    total = sum(rows.values()) or 1
    return {
        "buckets": [
            {"bucket": b, "count": rows.get(b, 0),
             "pct": round(100.0 * rows.get(b, 0) / total, 1)}
            for b in BUCKET_ORDER
        ],
        "total": total,
        **_stale(latest_date),
    }


def net_movement() -> dict:
    """
    Improved vs declined, compared per keyword between its own two most recent
    measurements — not between two calendar dates. See the module docstring:
    date-vs-date would count unmeasured keywords as movement.
    """
    row = fetch_one(
        """
        WITH ranked AS (
            SELECT keyword_id, date, rank_position,
                   ROW_NUMBER() OVER (PARTITION BY keyword_id ORDER BY date DESC) AS rn
              FROM keyword_rankings
             WHERE source <> 'gsc' AND rank_position IS NOT NULL
        ),
        pairs AS (
            SELECT c.keyword_id,
                   p.rank_position - c.rank_position AS gain,   -- positive = improved
                   c.date AS curr_date, p.date AS prev_date
              FROM ranked c JOIN ranked p
                ON p.keyword_id = c.keyword_id AND c.rn = 1 AND p.rn = 2
        )
        SELECT count(*) FILTER (WHERE gain > 0)  AS improved,
               count(*) FILTER (WHERE gain < 0)  AS declined,
               count(*) FILTER (WHERE gain = 0)  AS unchanged,
               round(avg(curr_date - prev_date))  AS avg_span_days,
               max(curr_date)                     AS latest
          FROM pairs
        """
    )
    if not row or row["improved"] is None:
        return {"improved": 0, "declined": 0, "unchanged": 0, **_stale(None)}

    top = fetch_all(
        """
        WITH ranked AS (
            SELECT keyword_id, date, rank_position,
                   ROW_NUMBER() OVER (PARTITION BY keyword_id ORDER BY date DESC) AS rn
              FROM keyword_rankings
             WHERE source <> 'gsc' AND rank_position IS NOT NULL
        )
        SELECT k.keyword, k.offering,
               p.rank_position AS was, c.rank_position AS now,
               p.rank_position - c.rank_position AS gain
          FROM ranked c
          JOIN ranked p ON p.keyword_id = c.keyword_id AND c.rn = 1 AND p.rn = 2
          JOIN keywords k ON k.id = c.keyword_id
         WHERE p.rank_position <> c.rank_position
         ORDER BY abs(p.rank_position - c.rank_position) DESC
         LIMIT 12
        """
    )
    return {
        "improved": row["improved"],
        "declined": row["declined"],
        "unchanged": row["unchanged"],
        "net": row["improved"] - row["declined"],
        "avg_span_days": int(row["avg_span_days"]) if row["avg_span_days"] else None,
        "movers": top,
        **_stale(row["latest"]),
    }


def search_console() -> dict:
    """Clicks, impressions, CTR and average position from the latest GSC pull."""
    latest = fetch_one(
        "SELECT max(date) AS d FROM keyword_rankings WHERE source = 'gsc'"
    )
    latest_date = latest["d"] if latest else None
    if not latest_date:
        return {"clicks": None, **_stale(None)}

    row = fetch_one(
        """
        SELECT count(DISTINCT keyword_id) AS keywords,
               COALESCE(sum(clicks), 0)      AS clicks,
               COALESCE(sum(impressions), 0) AS impressions,
               round(avg(rank_position)::numeric, 1) AS avg_position
          FROM keyword_rankings
         WHERE source = 'gsc' AND date = %s
        """,
        [latest_date],
    )
    impressions = int(row["impressions"] or 0)
    clicks = int(row["clicks"] or 0)
    return {
        "keywords": row["keywords"],
        "clicks": clicks,
        "impressions": impressions,
        # Carried as a real number, not a rounded string. 261/287,186 is 0.09% —
        # a rounded "0.1%" would hide how far below a normal CTR that sits.
        "ctr_pct": round(100.0 * clicks / impressions, 3) if impressions else None,
        "avg_position": float(row["avg_position"]) if row["avg_position"] else None,
        **_stale(latest_date),
    }


def striking_distance(limit: int = 15) -> dict:
    """Positions 11-20 on the latest cycle: the cheapest available wins."""
    latest = fetch_one(
        "SELECT max(date) AS d FROM keyword_rankings WHERE source <> 'gsc'"
    )
    latest_date = latest["d"] if latest else None
    if not latest_date:
        return {"count": 0, "keywords": [], **_stale(None)}

    count = fetch_one(
        """
        SELECT count(*) AS n FROM keyword_rankings
         WHERE source <> 'gsc' AND date = %s
           AND rank_position BETWEEN 11 AND 20
        """,
        [latest_date],
    )["n"]

    # Ordered by real search volume where we have it, then by position.
    #
    # NOT ordered by keywords.google_sv: that column is TEXT holding free-text
    # bands ('100 - 1K', '10-100', '1k- 10k', 'NA') with inconsistent dashes,
    # casing and spacing. Zero of its 652 non-null values parse as a number.
    # Coercing '100 - 1K' to 100 or 1000 would invent precision the data does
    # not have, so it is displayed as-is and never sorted on.
    #
    # keyword_search_volume holds proper integers but covers only 511 of 2,126
    # keywords, so `volume_rank` is NULL for most rows and they fall through to
    # position ordering. See the data-quality note in the dashboard README.
    rows = fetch_all(
        """
        WITH vol AS (
            SELECT DISTINCT ON (keyword_id) keyword_id, search_volume
              FROM keyword_search_volume
             ORDER BY keyword_id, date DESC
        )
        SELECT k.keyword, k.offering, r.rank_position AS position,
               v.search_volume, k.google_sv AS volume_band, k.target_url
          FROM keyword_rankings r
          JOIN keywords k ON k.id = r.keyword_id
          LEFT JOIN vol v ON v.keyword_id = k.id
         WHERE r.source <> 'gsc' AND r.date = %s
           AND r.rank_position BETWEEN 11 AND 20
         ORDER BY v.search_volume DESC NULLS LAST, r.rank_position
         LIMIT %s
        """,
        [latest_date, limit],
    )
    with_volume = fetch_one(
        """
        SELECT count(DISTINCT r.keyword_id) AS n
          FROM keyword_rankings r
          JOIN keyword_search_volume v ON v.keyword_id = r.keyword_id
         WHERE r.source <> 'gsc' AND r.date = %s
           AND r.rank_position BETWEEN 11 AND 20
        """,
        [latest_date],
    )["n"]
    return {
        "count": count,
        "with_known_volume": with_volume,
        "keywords": rows,
        **_stale(latest_date),
    }


def share_of_voice(limit: int = 10) -> dict:
    """
    Our top-10 coverage per offering, and the competitors taking the most
    top-10 slots. Two separate questions, deliberately not plotted on one
    axis — different measures, different scales.
    """
    latest = fetch_one(
        "SELECT max(date) AS d FROM keyword_rankings WHERE source <> 'gsc'"
    )
    latest_date = latest["d"] if latest else None

    ours = fetch_all(
        """
        SELECT k.offering,
               count(*) AS tracked,
               count(*) FILTER (WHERE r.rank_position <= 10) AS in_top_10
          FROM keyword_rankings r
          JOIN keywords k ON k.id = r.keyword_id
         WHERE r.source <> 'gsc' AND r.date = %s AND k.offering IS NOT NULL
         GROUP BY k.offering
         ORDER BY count(*) FILTER (WHERE r.rank_position <= 10) DESC
        """,
        [latest_date],
    ) if latest_date else []
    for o in ours:
        o["pct"] = round(100.0 * o["in_top_10"] / o["tracked"], 1) if o["tracked"] else 0.0

    competitors = fetch_all(
        """
        SELECT competitor_domain,
               sum(keywords_in_top_10) AS slots,
               count(DISTINCT offering) AS offerings,
               min(threat_tier) AS threat_tier
          FROM mv_offering_competition
         GROUP BY competitor_domain
         ORDER BY sum(keywords_in_top_10) DESC
         LIMIT %s
        """,
        [limit],
    )
    return {"by_offering": ours, "top_competitors": competitors, **_stale(latest_date)}


# ---------------------------------------------------------------------------
# Tier 2 — team
# ---------------------------------------------------------------------------

def coverage_gaps(limit: int = 12) -> dict:
    """Keywords not ranking at all, by offering. The biggest single bucket."""
    latest = fetch_one(
        "SELECT max(date) AS d FROM keyword_rankings WHERE source <> 'gsc'"
    )
    latest_date = latest["d"] if latest else None
    if not latest_date:
        return {"total": 0, "by_offering": [], **_stale(None)}

    rows = fetch_all(
        """
        SELECT k.offering,
               count(*) FILTER (WHERE r.rank_position IS NULL) AS missing,
               count(*) AS tracked
          FROM keyword_rankings r
          JOIN keywords k ON k.id = r.keyword_id
         WHERE r.source <> 'gsc' AND r.date = %s AND k.offering IS NOT NULL
         GROUP BY k.offering
         ORDER BY count(*) FILTER (WHERE r.rank_position IS NULL) DESC
         LIMIT %s
        """,
        [latest_date, limit],
    )
    total = fetch_one(
        """
        SELECT count(*) AS n FROM keyword_rankings
         WHERE source <> 'gsc' AND date = %s AND rank_position IS NULL
        """,
        [latest_date],
    )["n"]
    for r in rows:
        r["pct"] = round(100.0 * r["missing"] / r["tracked"], 1) if r["tracked"] else 0.0
    return {"total": total, "by_offering": rows, **_stale(latest_date)}


def technical_health() -> dict:
    """
    Open technical issues by severity.

    Four numbers is not a chart — rendered as counts with status dots. The
    severity names map onto the status palette, except `low`, which gets a
    neutral: a low-severity issue is not "good", it is merely not urgent.
    """
    rows = fetch_all(
        """
        SELECT severity, count(*) AS n, max(date_found) AS latest
          FROM technical_issues
         WHERE date_resolved IS NULL
         GROUP BY severity
        """
    )
    by_sev = {r["severity"]: r["n"] for r in rows}
    latest = max((r["latest"] for r in rows if r["latest"]), default=None)

    top_types = fetch_all(
        """
        SELECT issue_type, severity, count(*) AS n
          FROM technical_issues
         WHERE date_resolved IS NULL
         GROUP BY issue_type, severity
         ORDER BY count(*) DESC
         LIMIT 10
        """
    )
    resolved_30d = fetch_one(
        """
        SELECT count(*) AS n FROM technical_issues
         WHERE date_resolved >= CURRENT_DATE - INTERVAL '30 days'
        """
    )["n"]
    return {
        "open_total": sum(by_sev.values()),
        "by_severity": [
            {"severity": s, "count": by_sev.get(s, 0)}
            for s in ("critical", "high", "medium", "low")
        ],
        "top_types": top_types,
        "resolved_30d": resolved_30d,
        **_stale(latest),
    }


def core_web_vitals() -> dict:
    """Latest CWV sample per device, against the tenant's own pass marks."""
    thresholds = profile().policy("cwv_thresholds", {}) or {}
    rows = fetch_all(
        """
        WITH latest AS (
            SELECT url, device, performance_score, lcp_ms, inp_ms, cls_score, date,
                   ROW_NUMBER() OVER (PARTITION BY url, device ORDER BY date DESC) AS rn
              FROM cwv_metrics
        )
        SELECT device,
               count(*) AS urls,
               round(avg(performance_score)::numeric, 1) AS avg_score,
               round(avg(lcp_ms)::numeric)              AS avg_lcp_ms,
               max(date)                                AS latest
          FROM latest WHERE rn = 1
         GROUP BY device
        """
    )
    out = []
    latest = None
    for r in rows:
        bar = thresholds.get(r["device"])
        passing = fetch_one(
            """
            WITH latest AS (
                SELECT url, device, performance_score,
                       ROW_NUMBER() OVER (PARTITION BY url, device ORDER BY date DESC) AS rn
                  FROM cwv_metrics WHERE device = %s
            )
            SELECT count(*) FILTER (WHERE performance_score >= %s) AS ok, count(*) AS n
              FROM latest WHERE rn = 1
            """,
            [r["device"], bar or 0],
        ) if bar else None
        out.append({
            "device": r["device"],
            "urls": r["urls"],
            "avg_score": float(r["avg_score"]) if r["avg_score"] else None,
            "avg_lcp_ms": int(r["avg_lcp_ms"]) if r["avg_lcp_ms"] else None,
            "threshold": bar,
            "passing": passing["ok"] if passing else None,
            "pass_pct": round(100.0 * passing["ok"] / passing["n"], 1)
                        if passing and passing["n"] else None,
        })
        if r["latest"] and (latest is None or r["latest"] > latest):
            latest = r["latest"]
    return {"devices": out, "thresholds": thresholds, **_stale(latest)}


def candidate_queue(limit: int = 12) -> dict:
    """Discovered keywords awaiting a human decision. Promotion is gated."""
    counts = {
        r["status"]: r["n"]
        for r in fetch_all(
            "SELECT status, count(*) AS n FROM keyword_candidates GROUP BY status"
        )
    }
    rows = fetch_all(
        """
        SELECT id, display_keyword AS keyword, suggested_offering AS offering,
               trend_score, search_volume, momentum_ratio, mention_count
          FROM keyword_candidates
         WHERE status = 'new' AND is_novel
         ORDER BY trend_score DESC NULLS LAST
         LIMIT %s
        """,
        [limit],
    )
    latest = fetch_one("SELECT max(last_scored_date) AS d FROM keyword_candidates")
    return {
        "awaiting_review": counts.get("new", 0),
        "by_status": counts,
        "top": rows,
        **_stale(latest["d"] if latest else None),
    }


def competitor_movement(limit: int = 10) -> dict:
    """Competitors gaining ground, from the SERP event stream."""
    latest = fetch_one("SELECT max(event_date) AS d FROM competitor_serp_events")
    latest_date = latest["d"] if latest else None
    rows = fetch_all(
        """
        SELECT c.competitor_domain, c.threat_tier,
               count(*) FILTER (WHERE e.event_type = 'position_gain') AS gains,
               count(*) FILTER (WHERE e.event_type = 'position_drop') AS drops,
               count(*) FILTER (WHERE e.event_type = 'new_entrant')   AS entries,
               count(DISTINCT e.keyword_id) AS keywords
          FROM competitor_serp_events e
          JOIN competitors c ON c.id = e.competitor_id
         WHERE e.event_date >= %s::date - 30
           AND e.severity IN ('critical', 'high', 'medium')
         GROUP BY c.competitor_domain, c.threat_tier
         ORDER BY (count(*) FILTER (WHERE e.event_type = 'position_gain')
                 + count(*) FILTER (WHERE e.event_type = 'new_entrant')) DESC
         LIMIT %s
        """,
        [latest_date, limit],
    ) if latest_date else []
    return {"competitors": rows, "window_days": 30, **_stale(latest_date)}


def content_pipeline() -> dict:
    """Briefs and compliance checks. Currently near-empty — say so, don't hide it."""
    briefs = fetch_one(
        """
        SELECT count(*) AS total,
               count(*) FILTER (WHERE status = 'draft')     AS draft,
               count(*) FILTER (WHERE status = 'published')  AS published,
               max(created_at) AS latest
          FROM content_briefs
        """
    )
    checks = fetch_one(
        "SELECT count(*) AS total, max(check_date) AS latest FROM compliance_checks"
    )
    glossary_gap = fetch_one(
        """
        SELECT count(*) AS n FROM keywords
         WHERE status = 'active'
           AND (keyword ILIKE 'what is %%' OR keyword ILIKE 'what are %%'
                OR keyword ILIKE '%% meaning' OR keyword ILIKE 'define %%')
        """
    )["n"]
    return {
        "briefs_total": briefs["total"],
        "briefs_draft": briefs["draft"],
        "briefs_published": briefs["published"],
        "compliance_checks": checks["total"],
        "definitional_keywords": glossary_gap,
        **_stale(briefs["latest"] or checks["latest"]),
    }


def url_mismatch(limit: int = 15) -> dict:
    """
    Keywords where a different page ranks than the one assigned to them.

    18.6% of ranking keywords at time of writing — nearly one in five. Two
    different problems wear the same shape:

      * cannibalization — two of our pages compete and the weaker one wins
      * mis-assignment  — the assigned page was simply the wrong choice

    The data cannot tell them apart, so this is framed as "review", not "error".
    Sometimes the page that actually ranks is the better page and the assignment
    should change to match it.
    """
    latest = fetch_one(
        "SELECT max(date) AS d FROM keyword_rankings WHERE source <> 'gsc'"
    )
    latest_date = latest["d"] if latest else None
    if not latest_date:
        return {"count": 0, "keywords": [], **_stale(None)}

    counts = fetch_one(
        """
        SELECT count(*) AS ranking,
               count(*) FILTER (
                   WHERE k.target_url IS NOT NULL AND r.url_found IS NOT NULL
                     AND starts_with(k.target_url, 'http')
                     AND lower(rtrim(r.url_found, '/')) <> lower(rtrim(k.target_url, '/'))
               ) AS mismatched,
               -- A target_url that is not a URL is unassigned, not mismatched.
               -- Nine rows held the literal string 'NaN' from a spreadsheet
               -- import and would otherwise be permanent false positives in a
               -- list meant to be acted on. Migration 022 clears them; this
               -- guard keeps the tile honest if it happens again.
               count(*) FILTER (
                   WHERE k.target_url IS NULL
                      OR NOT starts_with(k.target_url, 'http')
               ) AS unassigned
          FROM keyword_rankings r
          JOIN keywords k ON k.id = r.keyword_id
         WHERE r.source <> 'gsc' AND r.rank_position IS NOT NULL AND r.date = %s
        """,
        [latest_date],
    )

    rows = fetch_all(
        """
        SELECT k.keyword, k.offering, r.rank_position AS position,
               k.target_url AS assigned_url, r.url_found AS ranking_url
          FROM keyword_rankings r
          JOIN keywords k ON k.id = r.keyword_id
         WHERE r.source <> 'gsc' AND r.date = %s
           AND r.rank_position IS NOT NULL
           AND k.target_url IS NOT NULL AND r.url_found IS NOT NULL
           AND starts_with(k.target_url, 'http')
           AND lower(rtrim(r.url_found, '/')) <> lower(rtrim(k.target_url, '/'))
         ORDER BY r.rank_position
         LIMIT %s
        """,
        [latest_date, limit],
    )
    ranking = counts["ranking"] or 0
    return {
        "count": counts["mismatched"] or 0,
        "ranking_total": ranking,
        "pct": round(100.0 * (counts["mismatched"] or 0) / ranking, 1) if ranking else 0.0,
        "unassigned": counts["unassigned"] or 0,
        "keywords": rows,
        **_stale(latest_date),
    }


# Impression floor for the demand-gap list. Below this, zero clicks is just a
# small sample rather than a missed opportunity.
DEMAND_GAP_MIN_IMPRESSIONS = 300
DEMAND_GAP_LOW_CTR_PCT = 0.5


def demand_gap(limit: int = 15) -> dict:
    """
    Keywords Google shows us for, that nobody clicks.

    The most directly actionable list on the dashboard. "cloud migration
    services" draws 2,608 impressions and zero clicks — that is demand already
    reaching the SERP and being handed to someone else.

    Two causes, separated here because the fix differs:
      * position too low to be clicked at all      -> ranking work
      * ranking but the snippet is not compelling  -> title/meta work

    Impressions and clicks come from GSC; position comes from the SERP snapshot
    so the two can disagree, which is itself informative.
    """
    gsc_latest = fetch_one(
        "SELECT max(date) AS d FROM keyword_rankings WHERE source = 'gsc'"
    )
    if not gsc_latest or not gsc_latest["d"]:
        return {"zero_click": [], "low_ctr": [], **_stale(None)}
    gd = gsc_latest["d"]

    serp_latest = fetch_one(
        "SELECT max(date) AS d FROM keyword_rankings WHERE source <> 'gsc'"
    )
    sd = serp_latest["d"] if serp_latest else None

    base = """
        SELECT k.keyword, k.offering,
               g.impressions, g.clicks, g.ctr,
               round(g.rank_position::numeric, 1) AS gsc_position,
               s.rank_position AS serp_position
          FROM keyword_rankings g
          JOIN keywords k ON k.id = g.keyword_id
          LEFT JOIN keyword_rankings s
                 ON s.keyword_id = k.id AND s.source <> 'gsc' AND s.date = %s
         WHERE g.source = 'gsc' AND g.date = %s
           AND g.impressions >= %s
    """

    zero_click = fetch_all(
        base + " AND COALESCE(g.clicks, 0) = 0 ORDER BY g.impressions DESC LIMIT %s",
        [sd, gd, DEMAND_GAP_MIN_IMPRESSIONS, limit],
    )
    low_ctr = fetch_all(
        base + """ AND COALESCE(g.clicks, 0) > 0
                   AND g.ctr * 100 < %s
              ORDER BY g.impressions DESC LIMIT %s""",
        [sd, gd, DEMAND_GAP_MIN_IMPRESSIONS, DEMAND_GAP_LOW_CTR_PCT, limit],
    )

    totals = fetch_one(
        """
        SELECT count(*) AS n,
               COALESCE(sum(impressions), 0) AS lost_impressions
          FROM keyword_rankings
         WHERE source = 'gsc' AND date = %s
           AND impressions >= %s AND COALESCE(clicks, 0) = 0
        """,
        [gd, DEMAND_GAP_MIN_IMPRESSIONS],
    )
    return {
        "zero_click": zero_click,
        "low_ctr": low_ctr,
        "zero_click_count": totals["n"],
        "lost_impressions": int(totals["lost_impressions"] or 0),
        "min_impressions": DEMAND_GAP_MIN_IMPRESSIONS,
        **_stale(gd),
    }


def channel_mix() -> dict:
    """
    Sessions and conversions by channel, so organic has a denominator.

    Included because it surfaced something the SEO-only view could not: AI
    Assistant traffic converts at roughly 4.9% against organic search's 1.6%.
    LLM referrals are a small channel that behaves far better than classic
    organic, and nothing else on the dashboard would have shown it.
    """
    latest = fetch_one("SELECT max(window_end) AS d FROM ga4_channel_totals")
    if not latest or not latest["d"]:
        return {"channels": [], **_stale(None)}
    d = latest["d"]

    rows = fetch_all(
        """
        SELECT channel,
               sum(sessions)         AS sessions,
               sum(engaged_sessions) AS engaged_sessions,
               sum(conversions)      AS conversions,
               sum(revenue)          AS revenue,
               count(DISTINCT domain) AS domains
          FROM ga4_channel_totals
         WHERE window_end = %s
         GROUP BY channel
         ORDER BY sum(sessions) DESC
        """,
        [d],
    )
    window = fetch_one(
        "SELECT max(window_days) AS w FROM ga4_channel_totals WHERE window_end = %s", [d]
    )
    total_sessions = sum(int(r["sessions"] or 0) for r in rows) or 1
    out = []
    for r in rows:
        s = int(r["sessions"] or 0)
        c = float(r["conversions"] or 0)
        out.append({
            "channel": r["channel"],
            "sessions": s,
            "conversions": c,
            "domains": r["domains"],
            "share_pct": round(100.0 * s / total_sessions, 1),
            "cvr_pct": round(100.0 * c / s, 2) if s else None,
        })

    organic = next((c for c in out if c["channel"].lower() == "organic search"), None)
    ai = next((c for c in out if "ai" in c["channel"].lower()
               and "assistant" in c["channel"].lower()), None)
    return {
        "channels": out,
        "total_sessions": total_sessions,
        "window_days": window["w"] if window else None,
        "organic": organic,
        "ai_assistant": ai,
        # Stated as a ratio rather than left for the reader to divide, because
        # this is the comparison that makes the tile worth having.
        "ai_vs_organic_cvr": (
            round(ai["cvr_pct"] / organic["cvr_pct"], 1)
            if ai and organic and organic.get("cvr_pct") else None
        ),
        **_stale(d),
    }


def attribution_coverage() -> dict:
    """
    How much of the conversion data can actually be assigned to an offering.

    Qualifies every other conversion number on the page. Roughly a quarter is
    attributable today: most converting URLs have no `pages` row at all, and
    the biggest single converter is the homepage, which has no offering by
    nature. Without this tile the per-offering table reads as a verdict on the
    offerings rather than on the mapping.
    """
    latest = fetch_one("SELECT max(window_end) AS d FROM ga4_landing_pages")
    if not latest or not latest["d"]:
        return {"total": 0, **_stale(None)}
    d = latest["d"]

    r = fetch_one(
        """
        SELECT
          COALESCE(sum(g.conversions), 0) AS total,
          COALESCE(sum(g.conversions) FILTER (WHERE p.offering IS NOT NULL), 0) AS attributed,
          COALESCE(sum(g.conversions) FILTER (
              WHERE g.page_id IS NOT NULL AND p.offering IS NULL), 0) AS page_no_offering,
          COALESCE(sum(g.conversions) FILTER (WHERE g.page_id IS NULL), 0) AS no_page,
          count(*) FILTER (WHERE g.page_id IS NULL) AS unmatched_pages,
          count(*) AS landing_pages
          FROM ga4_landing_pages g
          LEFT JOIN pages p ON p.id = g.page_id
         WHERE g.window_end = %s AND g.channel = 'Organic Search'
        """,
        [d],
    )
    total = float(r["total"] or 0)
    pages_with_offering = fetch_one(
        "SELECT count(*) AS n FROM pages WHERE offering IS NOT NULL")["n"]
    pages_total = fetch_one("SELECT count(*) AS n FROM pages")["n"]

    return {
        "total": total,
        "attributed": float(r["attributed"] or 0),
        "page_no_offering": float(r["page_no_offering"] or 0),
        "no_page": float(r["no_page"] or 0),
        "coverage_pct": round(100.0 * float(r["attributed"] or 0) / total, 1) if total else 0.0,
        "unmatched_pages": r["unmatched_pages"],
        "landing_pages": r["landing_pages"],
        "pages_with_offering": pages_with_offering,
        "pages_total": pages_total,
        **_stale(d),
    }


# ---------------------------------------------------------------------------
# Tier 3 — system health
# ---------------------------------------------------------------------------

def agent_health() -> dict:
    """
    Per-agent freshness from v_agent_status.

    This tier is not decoration. With half the source data months old, a
    dashboard that cannot show which agent stopped running is presenting
    stale numbers as current.
    """
    rows = fetch_all(
        """
        SELECT name, title, folder, kind, health, blocked_by,
               cadence_days, last_run, last_status, never_run
          FROM v_agent_status
         ORDER BY CASE health
                    WHEN 'overdue'   THEN 0
                    WHEN 'blocked'   THEN 1
                    WHEN 'never run' THEN 2
                    WHEN 'current'   THEN 3
                    ELSE 4 END,
                  name
        """
    )
    for r in rows:
        r["last_run"] = r["last_run"].date().isoformat() if r["last_run"] else None
    summary: dict[str, int] = {}
    for r in rows:
        summary[r["health"]] = summary.get(r["health"], 0) + 1
    return {"agents": rows, "summary": summary}


def run_activity(days: int = 30) -> dict:
    """Recent agent runs and spend, best-effort across metadata key spellings."""
    runs = fetch_all(
        """
        SELECT agent_name, run_date, status, records_processed, duration_seconds,
               COALESCE(
                   (metadata->>'cost_usd')::numeric,
                   (metadata->>'estimated_cost_usd')::numeric,
                   (metadata->>'llm_cost_usd')::numeric,
                   (metadata->>'api_cost_usd')::numeric,
                   0
               ) AS cost_usd
          FROM agent_runs
         WHERE run_date >= CURRENT_DATE - %s::int
         ORDER BY run_date DESC
         LIMIT 40
        """,
        [days],
    )
    for r in runs:
        r["run_date"] = r["run_date"].isoformat()
        r["cost_usd"] = float(r["cost_usd"] or 0)
    totals = {
        "runs": len(runs),
        "failed": sum(1 for r in runs if r["status"] in ("failed", "error")),
        "partial": sum(1 for r in runs if r["status"] == "partial"),
        "cost_usd": round(sum(r["cost_usd"] for r in runs), 2),
    }
    return {"runs": runs, "totals": totals, "window_days": days}


# ---------------------------------------------------------------------------
# Aggregate
# ---------------------------------------------------------------------------

TILES = {
    "visibility":            visibility,
    "position_distribution": position_distribution,
    "net_movement":          net_movement,
    "search_console":        search_console,
    "striking_distance":     striking_distance,
    "share_of_voice":        share_of_voice,
    "coverage_gaps":         coverage_gaps,
    "url_mismatch":          url_mismatch,
    "demand_gap":            demand_gap,
    "channel_mix":           channel_mix,
    "attribution_coverage":  attribution_coverage,
    "technical_health":      technical_health,
    "core_web_vitals":       core_web_vitals,
    "candidate_queue":       candidate_queue,
    "competitor_movement":   competitor_movement,
    "content_pipeline":      content_pipeline,
    "agent_health":          agent_health,
    "run_activity":          run_activity,
}


def all_tiles() -> dict:
    """
    Every tile. One failing query degrades its own tile rather than the page —
    a dashboard that 500s because one aggregate broke is less useful than one
    that renders eleven tiles and an error on the twelfth.
    """
    out: dict[str, Any] = {}
    for name, fn in TILES.items():
        try:
            out[name] = fn()
        except Exception as exc:
            logger.exception("KPI %s failed", name)
            out[name] = {"error": f"{type(exc).__name__}: {exc}"}
    p = profile()
    out["_meta"] = {
        "brand": p.brand_name,
        "primary_domain": p.primary_domain,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "offerings": list(p.offering_names),
    }
    return out
