"""
Concentration Checker — Phase 2 module of Content Operations
=============================================================

Analyzes the distribution of content briefs and published pages over a
rolling window. Flags when any single bucket — offering, audience stage,
keyword intent, or page type — exceeds a configured threshold of total
output. The goal is to prevent SEO blind spots where the calendar
over-indexes on one slice and starves the others.

Why this matters
----------------
Damco has 14 offerings. If 70% of new briefs go to "AI" because that's
where the team's attention is, "Insurance" / "Microsoft" / "OutSystems"
build up coverage debt over months. By the time anyone notices via
keyword rankings, the recovery cost is months of catch-up work.

This module surfaces the imbalance early: every time a fresh batch of
briefs is generated, run this to check the rolling distribution.

What gets checked
-----------------
For a configurable rolling window (default: 90 days), we compute the
share of new briefs across these dimensions:
  - offering            (which Damco service line)
  - audience_stage      (awareness / consideration / decision)
  - page_type           (service / pillar / blog / landing / glossary)
  - intent              (informational / commercial / transactional)

Any dimension where one bucket exceeds `--threshold` (default 40%) of
total output produces a warning. Dimensions where two buckets account
for >70% combined also flag as "narrow concentration".

Outputs
-------
- outputs/audits/concentration_<date>.md   (narrative + concrete remedies)
- agent_runs row (metadata stores the distribution snapshot)

Usage
-----
    # Default — 90-day window, 40% threshold, all dimensions
    python -m content_operations.concentration_checker

    # Tighter threshold
    python -m content_operations.concentration_checker --threshold 30

    # Different rolling window
    python -m content_operations.concentration_checker --days 60

    # Restrict to one dimension
    python -m content_operations.concentration_checker --dimension offering

    # Dry run — write report, skip agent_runs DB row
    python -m content_operations.concentration_checker --dry-run

Design notes
------------
Rule-based and free — pure SQL aggregation + Python tabulation. The
brief_content JSONB stores audience_stage / page_type / offering inside
each brief; we read those out without joining elsewhere.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from collections import Counter, defaultdict
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from common.database import fetch_all, record_agent_run


logger = logging.getLogger("concentration_checker")
AGENT_NAME = "content_operations.concentration_checker"

OUTPUT_DIR = Path(__file__).resolve().parent.parent / "outputs" / "audits"

DEFAULT_DAYS = 90
DEFAULT_THRESHOLD_PCT = 40.0          # any one bucket above this = warn
NARROW_TWO_BUCKET_PCT = 70.0          # top two buckets combined above this = warn
MIN_BRIEFS_FOR_ANALYSIS = 5           # below this, distribution is noise

DIMENSIONS = ("offering", "audience_stage", "page_type", "intent")


# ---------------------------------------------------------------------------
# Read phase
# ---------------------------------------------------------------------------

def load_recent_briefs(days: int) -> list[dict]:
    """
    Pull briefs created in the rolling window with the dimensions we
    care about — pulled out of brief_content JSONB so we don't need
    extra columns.
    """
    cutoff = date.today() - timedelta(days=days)
    return fetch_all(
        """
        SELECT id,
               created_at::date              AS date_created,
               status,
               brief_content -> 'target' ->> 'offering'        AS offering,
               brief_content -> 'audience' ->> 'stage'         AS audience_stage,
               brief_content -> 'target' ->> 'page_type'       AS page_type,
               brief_content -> 'target' ->> 'intent'          AS intent
          FROM content_briefs
         WHERE created_at::date >= %s
         ORDER BY created_at
        """,
        [cutoff],
    )


# ---------------------------------------------------------------------------
# Distribution analysis
# ---------------------------------------------------------------------------

def bucket_counts(briefs: list[dict], dimension: str) -> Counter:
    """Count briefs per bucket on a given dimension. Empty buckets → '(unspecified)'."""
    c: Counter = Counter()
    for b in briefs:
        v = b.get(dimension)
        if v is None or (isinstance(v, str) and not v.strip()):
            v = "(unspecified)"
        c[v] += 1
    return c


def analyze_dimension(briefs: list[dict], dimension: str,
                      threshold_pct: float) -> dict:
    """
    Returns:
        {
          "dimension": "offering",
          "total": N,
          "buckets": [{name, count, pct}, ...]     (sorted desc),
          "issues": [str, str],                     (flags raised)
          "top_pct": float,
          "top2_pct": float,
        }
    """
    counts = bucket_counts(briefs, dimension)
    total = sum(counts.values())
    if total == 0:
        return {"dimension": dimension, "total": 0, "buckets": [], "issues": [],
                "top_pct": 0.0, "top2_pct": 0.0}

    items = counts.most_common()
    buckets = [{
        "name":  name,
        "count": cnt,
        "pct":   round(100.0 * cnt / total, 1),
    } for name, cnt in items]

    issues: list[str] = []
    top_pct = buckets[0]["pct"]
    top2_pct = round(buckets[0]["pct"] + (buckets[1]["pct"] if len(buckets) > 1 else 0), 1)

    if top_pct > threshold_pct:
        issues.append(
            f"`{buckets[0]['name']}` accounts for {top_pct}% of {dimension} output "
            f"(threshold: {threshold_pct}%)."
        )

    if len(buckets) >= 2 and top2_pct > NARROW_TWO_BUCKET_PCT:
        issues.append(
            f"Top two buckets (`{buckets[0]['name']}` + `{buckets[1]['name']}`) "
            f"are {top2_pct}% of output — narrow distribution."
        )

    # Missing-coverage flag: if dimension is "offering", surface offerings
    # tracked in `keywords` but absent from briefs.
    if dimension == "offering" and briefs:
        all_active = {
            r["offering"] for r in fetch_all(
                "SELECT DISTINCT offering FROM keywords "
                "WHERE status = 'active' AND offering IS NOT NULL"
            )
        }
        covered = {b["name"] for b in buckets if b["name"] != "(unspecified)"}
        missing = sorted(all_active - covered)
        if missing:
            issues.append(
                f"Offerings with 0 briefs in window: {', '.join(missing[:6])}"
                + (f" (+{len(missing)-6} more)" if len(missing) > 6 else "")
            )

    return {
        "dimension": dimension,
        "total":     total,
        "buckets":   buckets,
        "issues":    issues,
        "top_pct":   top_pct,
        "top2_pct":  top2_pct,
    }


# ---------------------------------------------------------------------------
# Recommendations
# ---------------------------------------------------------------------------

def generate_recommendations(analyses: list[dict], briefs: list[dict],
                              days: int) -> list[str]:
    """Concrete next-steps. Each recommendation is actionable."""
    recs: list[str] = []

    by_dim = {a["dimension"]: a for a in analyses}

    # 1. Over-concentrated offering → suggest where to rebalance
    off = by_dim.get("offering")
    if off and off["issues"]:
        underweight = [b["name"] for b in off["buckets"][-3:] if b["name"] != "(unspecified)"]
        if underweight:
            recs.append(
                f"Run `brief_generator --coverage-gap --offering \"{underweight[0]}\" --limit 5` "
                f"to start re-balancing into underweight offerings ({', '.join(underweight)})."
            )

    # 2. Audience stage skew
    aud = by_dim.get("audience_stage")
    if aud:
        stages = {b["name"]: b["pct"] for b in aud["buckets"]}
        if stages.get("decision", 0) > 60:
            recs.append("Pipeline is decision-stage heavy — Damco lacks awareness/consideration content. "
                        "Add 'what is X', 'X explained' keyword variants and run `glossary_detector`.")
        elif stages.get("awareness", 0) > 60:
            recs.append("Pipeline is awareness-stage heavy — light on commercial intent. "
                        "Make sure each awareness page funnels into a decision-stage service page via internal links.")

    # 3. Page-type concentration
    pt = by_dim.get("page_type")
    if pt:
        types = {b["name"]: b["pct"] for b in pt["buckets"]}
        if types.get("service", 0) > 80:
            recs.append("100% service pages — add pillar pages (1500+ words) to anchor topical authority, "
                        "and glossary pages for AI search citation surface.")
        if types.get("blog", 0) == 0 and pt["total"] >= 5:
            recs.append("No blog content in the window. Blogs convert search traffic to MQLs at a different "
                        "funnel point; consider 1-2 per offering per quarter.")

    # 4. Volume-too-low check
    if not briefs or len(briefs) < MIN_BRIEFS_FOR_ANALYSIS:
        recs.append(f"Only {len(briefs)} brief(s) in the {days}-day window — "
                    f"distribution is noisy. Generate more briefs before reading much into the shape.")

    return recs


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def write_markdown(briefs: list[dict], analyses: list[dict],
                   recommendations: list[str], days: int,
                   threshold_pct: float) -> Path:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUTPUT_DIR / f"concentration_{date.today().isoformat()}.md"

    p: list[str] = []
    p.append(f"# Content Calendar Concentration — {date.today().isoformat()}")
    p.append("")
    p.append(f"_Window: rolling **{days} days** (since {(date.today() - timedelta(days=days)).isoformat()})_")
    p.append(f"_Threshold: any single bucket >{threshold_pct}% of output_")
    p.append(f"_Generated by `{AGENT_NAME}`._")
    p.append("")

    p.append("## Summary")
    p.append("")
    p.append("| Metric | Value |")
    p.append("|---|---:|")
    p.append(f"| Briefs in window | {len(briefs)} |")
    flagged = [a for a in analyses if a["issues"]]
    p.append(f"| Dimensions flagged | {len(flagged)} / {len(analyses)} |")
    p.append("")

    if not briefs:
        p.append("_No briefs in the window. Generate some via `brief_generator` first._")
        path.write_text("\n".join(p), encoding="utf-8")
        return path

    if flagged:
        p.append("## ⚠️ Concentration flags")
        p.append("")
        for a in flagged:
            p.append(f"### `{a['dimension']}`  (total: {a['total']} briefs)")
            p.append("")
            for issue in a["issues"]:
                p.append(f"- ⚠️ {issue}")
            p.append("")
    else:
        p.append("## ✅ No concentration issues detected.")
        p.append("")

    p.append("## Distribution by dimension")
    p.append("")
    for a in analyses:
        p.append(f"### {a['dimension']}")
        p.append("")
        if not a["buckets"]:
            p.append("_(no data)_")
            p.append("")
            continue
        p.append("| Bucket | Count | Share |")
        p.append("|---|---:|---:|")
        for b in a["buckets"][:15]:
            p.append(f"| `{b['name']}` | {b['count']} | {b['pct']}% |")
        if len(a["buckets"]) > 15:
            p.append(f"| _… and {len(a['buckets']) - 15} more_ |  |  |")
        p.append("")

    if recommendations:
        p.append("## Recommended actions")
        p.append("")
        for r in recommendations:
            p.append(f"- {r}")
        p.append("")

    # Recent briefs table — gives the reviewer a feel for what's been produced
    p.append("## Recent briefs in this window")
    p.append("")
    p.append("| Date | Status | Offering | Stage | Page type |")
    p.append("|---|---|---|---|---|")
    for b in briefs[-20:]:
        p.append(f"| {b['date_created']} | {b['status']} | "
                 f"{b.get('offering') or '—'} | "
                 f"{b.get('audience_stage') or '—'} | "
                 f"{b.get('page_type') or '—'} |")
    if len(briefs) > 20:
        p.append(f"| _… {len(briefs) - 20} earlier briefs not shown_ |")
    p.append("")

    path.write_text("\n".join(p), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(days: int = DEFAULT_DAYS,
        threshold_pct: float = DEFAULT_THRESHOLD_PCT,
        dimension: str | None = None,
        dry_run: bool = False) -> dict:
    start = time.monotonic()

    briefs = load_recent_briefs(days)
    logger.info("Loaded %d brief(s) from the last %d days", len(briefs), days)

    dims_to_run = [dimension] if dimension else list(DIMENSIONS)
    analyses = [analyze_dimension(briefs, d, threshold_pct) for d in dims_to_run]
    recommendations = generate_recommendations(analyses, briefs, days)

    md_path = write_markdown(briefs, analyses, recommendations, days, threshold_pct)

    duration = time.monotonic() - start
    flagged_count = sum(1 for a in analyses if a["issues"])

    if not dry_run:
        record_agent_run(
            agent_name=AGENT_NAME,
            status="success" if not flagged_count else "partial",
            records_processed=len(briefs),
            errors=[],
            duration_seconds=round(duration, 2),
            metadata={
                "run_date":          date.today().isoformat(),
                "days":              days,
                "threshold_pct":     threshold_pct,
                "dimension_filter":  dimension,
                "brief_count":       len(briefs),
                "flagged_dimensions": [a["dimension"] for a in analyses if a["issues"]],
                "distribution":      {a["dimension"]: a["buckets"][:5] for a in analyses},
                "md_path":           str(md_path),
            },
        )

    # Console summary
    print()
    print(f"  {'=' * 72}")
    print(f"   CONCENTRATION CHECKER -- {date.today().isoformat()}")
    print(f"  {'=' * 72}")
    print()
    print(f"  Window:              last {days} days")
    print(f"  Briefs in window:    {len(briefs)}")
    print(f"  Threshold:           >{threshold_pct}% of any bucket")
    print(f"  Dimensions flagged:  {flagged_count} / {len(analyses)}")
    for a in analyses:
        marker = "[FLAG] " if a["issues"] else "       "
        leader = a["buckets"][0] if a["buckets"] else None
        if leader:
            print(f"  {marker}{a['dimension']:<18} leader: `{leader['name']}` ({leader['pct']}%, "
                  f"total {a['total']})")
        else:
            print(f"  {marker}{a['dimension']:<18} (no data)")
    print(f"  Report:              {md_path}")
    print(f"  Duration:            {duration:.2f}s")
    if recommendations:
        print()
        print(f"  Recommendations:")
        for r in recommendations[:5]:
            print(f"    - {r[:120]}")
    print()

    return {
        "status":           "success" if not flagged_count else "partial",
        "brief_count":      len(briefs),
        "flagged":          flagged_count,
        "md_path":          str(md_path),
        "duration_seconds": round(duration, 2),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Damco Content Calendar Concentration Checker")
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS,
                        help=f"Rolling window in days (default: {DEFAULT_DAYS})")
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD_PCT,
                        help=f"Max % of output for any single bucket (default: {DEFAULT_THRESHOLD_PCT})")
    parser.add_argument("--dimension", choices=DIMENSIONS,
                        help="Restrict analysis to one dimension (default: all)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Generate report but skip agent_runs DB row")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s  %(name)s  %(levelname)s  %(message)s",
    )

    run(days=args.days,
        threshold_pct=args.threshold,
        dimension=args.dimension,
        dry_run=args.dry_run)


if __name__ == "__main__":
    main()
