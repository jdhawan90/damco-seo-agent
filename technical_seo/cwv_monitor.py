"""
Core Web Vitals Monitor — Phase 2 of Technical SEO agent
=========================================================

Standard agent lifecycle:
  Read    — fetch eligible pages from `pages` table (page_type filter,
            cadence-aware per (url, device))
  Process — call PageSpeed Insights for each (url, mobile|desktop);
            compute regressions vs the previous snapshot
  Write   — upsert `cwv_metrics`; open/resolve `technical_issues` for
            below-threshold scores and 20%+ regressions; log `agent_runs`
  Notify  — console summary

Thresholds (per session 2026-05-05 plan):
  Mobile  performance score < 60  → cwv_below_threshold (severity high)
  Desktop performance score < 85  → cwv_below_threshold (severity high)

Regression detection:
  Any of {lcp_ms, inp_ms, cls_score, performance_score} drops by ≥20%
  vs the most recent previous snapshot for the same (url, device)
  → cwv_regression (severity medium).

Usage
-----
    # Default: all 3 domains, page_type IN (home, pillar, service),
    # weekly cadence, both mobile + desktop, 4 parallel workers.
    python -m technical_seo.cwv_monitor

    # Restrict to one domain
    python -m technical_seo.cwv_monitor --domain damcogroup.com

    # Cover blog + resource pages too (much larger run)
    python -m technical_seo.cwv_monitor --page-types home,pillar,service,blog,resource

    # Force re-check ignoring cadence
    python -m technical_seo.cwv_monitor --all

    # Dry run — call PageSpeed but don't write to DB
    python -m technical_seo.cwv_monitor --dry-run

Notes
-----
- Without PAGESPEED_API_KEY in .env, PageSpeed allows 25 queries / 100s.
  The default 4 workers + ~12s/call means we naturally stay under that.
- Cadence is per (url, device). If mobile was checked yesterday but
  desktop wasn't, only desktop runs.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from common.database import connection, fetch_all, record_agent_run
from common.connectors.pagespeed import get_cwv_metrics, PageSpeedError


logger = logging.getLogger("cwv_monitor")

AGENT_NAME = "technical_seo.cwv_monitor"

# Performance score thresholds (session 2026-05-05 plan).
THRESHOLDS = {
    "mobile":  60,
    "desktop": 85,
}

# Regression: 20% drop in any tracked metric vs previous snapshot.
REGRESSION_PCT = 0.20

DEFAULT_PAGE_TYPES = ("home", "pillar", "service")
DEFAULT_CADENCE_DAYS = 7
DEFAULT_WORKERS = 4
DEFAULT_STRATEGIES = ("mobile", "desktop")

ISSUE_SEVERITY = {
    "cwv_below_threshold": "high",
    "cwv_regression":      "medium",
}


# ---------------------------------------------------------------------------
# Read phase
# ---------------------------------------------------------------------------

def load_pages(domain: str | None, page_types: tuple[str, ...]) -> list[str]:
    """Active pages matching the page_type filter."""
    sql = "SELECT url FROM pages WHERE page_type = ANY(%s)"
    params: list = [list(page_types)]
    if domain:
        sql += " AND url LIKE %s"
        params.append(f"%{domain}%")
    sql += " ORDER BY url"
    return [r["url"] for r in fetch_all(sql, params)]


def filter_due_work_items(urls: list[str], strategies: tuple[str, ...],
                          cadence_days: int) -> list[tuple[str, str]]:
    """
    Generate (url, strategy) work items, filtered to ones whose latest
    cwv_metrics for that (url, device) is older than cadence_days (or absent).
    """
    if not urls:
        return []

    rows = fetch_all(
        """
        SELECT url, device, MAX(date) AS last_date
          FROM cwv_metrics
         WHERE url = ANY(%s)
         GROUP BY url, device
        """,
        [urls],
    )
    last_seen = {(r["url"], r["device"]): r["last_date"] for r in rows}

    today = date.today()
    work: list[tuple[str, str]] = []
    for url in urls:
        for strategy in strategies:
            last = last_seen.get((url, strategy))
            if last is None or (today - last).days >= cadence_days:
                work.append((url, strategy))
    return work


def load_previous_metrics(work_items: list[tuple[str, str]]) -> dict[tuple[str, str], dict]:
    """For regression baseline: latest snapshot per (url, device) from before today."""
    if not work_items:
        return {}
    urls = list({u for u, _ in work_items})
    rows = fetch_all(
        """
        SELECT m.url, m.device, m.lcp_ms, m.inp_ms, m.cls_score, m.performance_score, m.date
          FROM cwv_metrics m
          JOIN (
              SELECT url, device, MAX(date) AS max_date
                FROM cwv_metrics
               WHERE url = ANY(%s) AND date < CURRENT_DATE
               GROUP BY url, device
          ) latest ON latest.url = m.url AND latest.device = m.device AND latest.max_date = m.date
        """,
        [urls],
    )
    return {(r["url"], r["device"]): r for r in rows}


# ---------------------------------------------------------------------------
# Process phase
# ---------------------------------------------------------------------------

def fetch_one(url: str, strategy: str) -> dict:
    """Single PageSpeed call. Returns {url, strategy, ...metrics, error}."""
    try:
        m = get_cwv_metrics(url, strategy=strategy)  # type: ignore[arg-type]
        return {**m, "error": None}
    except PageSpeedError as exc:
        return {
            "url": url, "strategy": strategy,
            "performance_score": None, "lcp_ms": None, "inp_ms": None, "cls": None,
            "source": None, "error": str(exc),
        }


def fetch_all_metrics(work_items: list[tuple[str, str]], workers: int) -> list[dict]:
    """Parallel PageSpeed fetch. Order of returns is non-deterministic."""
    results: list[dict] = []
    if not work_items:
        return results

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {
            ex.submit(fetch_one, url, strategy): (url, strategy)
            for url, strategy in work_items
        }
        for i, fut in enumerate(as_completed(futures), 1):
            url, strategy = futures[fut]
            if i % 5 == 0 or i == len(work_items):
                logger.info("  fetched %d/%d", i, len(work_items))
            try:
                results.append(fut.result())
            except Exception as exc:
                results.append({
                    "url": url, "strategy": strategy,
                    "performance_score": None, "lcp_ms": None, "inp_ms": None, "cls": None,
                    "source": None, "error": str(exc),
                })
    return results


def compute_regressions(prev: dict | None, curr: dict) -> list[dict]:
    """
    Compare current metrics to previous snapshot. Returns a list of
    regression descriptors. Empty if no prev or no regressions.
    """
    if not prev:
        return []

    regressions: list[dict] = []

    # Lower-is-better metrics: lcp_ms, inp_ms, cls_score
    for metric_key, curr_key in [
        ("lcp_ms",    "lcp_ms"),
        ("inp_ms",    "inp_ms"),
        ("cls_score", "cls"),  # connector returns "cls"; DB column is "cls_score"
    ]:
        prev_v = prev.get(metric_key)
        curr_v = curr.get(curr_key)
        if prev_v is None or curr_v is None or prev_v == 0:
            continue
        delta_pct = (curr_v - prev_v) / prev_v
        if delta_pct >= REGRESSION_PCT:
            regressions.append({
                "metric": metric_key, "previous": prev_v, "current": curr_v,
                "delta_pct": round(delta_pct * 100, 1),
                "direction": "regression",
            })

    # Higher-is-better metric: performance_score
    prev_v = prev.get("performance_score")
    curr_v = curr.get("performance_score")
    if prev_v is not None and curr_v is not None and prev_v > 0:
        delta_pct = (curr_v - prev_v) / prev_v
        if delta_pct <= -REGRESSION_PCT:
            regressions.append({
                "metric": "performance_score", "previous": prev_v, "current": curr_v,
                "delta_pct": round(delta_pct * 100, 1),
                "direction": "regression",
            })

    return regressions


# ---------------------------------------------------------------------------
# Write phase
# ---------------------------------------------------------------------------

def upsert_cwv_metric(cur, *, url: str, strategy: str, run_date: date,
                      perf: int | None, lcp: int | None, inp: int | None,
                      cls: float | None) -> None:
    cur.execute(
        """
        INSERT INTO cwv_metrics (url, date, lcp_ms, inp_ms, cls_score,
                                 performance_score, device)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (url, date, device) DO UPDATE SET
            lcp_ms            = EXCLUDED.lcp_ms,
            inp_ms            = EXCLUDED.inp_ms,
            cls_score         = EXCLUDED.cls_score,
            performance_score = EXCLUDED.performance_score
        """,
        (url, run_date, lcp, inp, cls, perf, strategy),
    )


def find_open_issue(cur, *, url: str, issue_type: str, device: str) -> int | None:
    """Returns issue id if an open issue exists for this (url, type, device)."""
    cur.execute(
        """
        SELECT id FROM technical_issues
         WHERE url = %s AND issue_type = %s AND date_resolved IS NULL
           AND details->>'device' = %s
         LIMIT 1
        """,
        (url, issue_type, device),
    )
    row = cur.fetchone()
    return row[0] if row and not isinstance(row, dict) else (row["id"] if row else None)


def open_issue(cur, *, url: str, issue_type: str, severity: str,
               device: str, details: dict) -> bool:
    """Insert iff no open (url, type, device) issue. Returns True if inserted."""
    if find_open_issue(cur, url=url, issue_type=issue_type, device=device):
        return False
    payload = {**details, "device": device}
    cur.execute(
        """
        INSERT INTO technical_issues (url, issue_type, severity, details)
        VALUES (%s, %s, %s, %s::jsonb)
        """,
        (url, issue_type, severity, json.dumps(payload)),
    )
    return True


def resolve_stale_issues(cur, *, current_open: set[tuple[str, str, str]],
                         issue_types: list[str], urls_checked: set[str]) -> int:
    """
    Resolve any (url, issue_type, device) NOT in current_open, scoped to
    URLs we actually checked this run (don't touch issues for unchecked URLs).
    """
    if not urls_checked:
        return 0
    cur.execute(
        """
        SELECT id, url, issue_type, details
          FROM technical_issues
         WHERE date_resolved IS NULL
           AND issue_type = ANY(%s)
           AND url = ANY(%s)
        """,
        (issue_types, list(urls_checked)),
    )
    resolved = 0
    for row in cur.fetchall():
        rid    = row[0] if not isinstance(row, dict) else row["id"]
        url    = row[1] if not isinstance(row, dict) else row["url"]
        itype  = row[2] if not isinstance(row, dict) else row["issue_type"]
        det    = row[3] if not isinstance(row, dict) else row["details"]
        device = (det or {}).get("device") if isinstance(det, dict) else None
        if device is None:
            continue
        if (url, itype, device) in current_open:
            continue
        cur.execute(
            "UPDATE technical_issues SET date_resolved = now() WHERE id = %s",
            (rid,),
        )
        resolved += 1
    return resolved


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def write_run_results(results: list[dict], previous: dict[tuple[str, str], dict],
                      run_date: date, dry_run: bool) -> dict:
    """Iterate over fetched results; write metrics + issues; return counters."""
    counters = {
        "metrics_written":      0,
        "below_threshold_open": 0,
        "regression_open":      0,
        "issues_resolved":      0,
        "fetch_errors":         0,
    }
    current_open: set[tuple[str, str, str]] = set()
    urls_checked: set[str] = set()

    if dry_run:
        for r in results:
            urls_checked.add(r["url"])
            if r.get("error"):
                counters["fetch_errors"] += 1
                continue
            score = r.get("performance_score")
            device = r["strategy"]
            if score is not None and score < THRESHOLDS[device]:
                counters["below_threshold_open"] += 1
            regs = compute_regressions(previous.get((r["url"], device)), r)
            if regs:
                counters["regression_open"] += 1
        return counters

    with connection() as conn:
        cur = conn.cursor()
        for r in results:
            url = r["url"]
            device = r["strategy"]
            urls_checked.add(url)

            if r.get("error"):
                counters["fetch_errors"] += 1
                continue

            perf = r.get("performance_score")
            lcp  = r.get("lcp_ms")
            inp  = r.get("inp_ms")
            cls  = r.get("cls")

            # 1. Write metric row (even if some values are None — capture the snapshot)
            try:
                upsert_cwv_metric(
                    cur, url=url, strategy=device, run_date=run_date,
                    perf=perf, lcp=lcp, inp=inp, cls=cls,
                )
                counters["metrics_written"] += 1
            except Exception as exc:
                logger.error("Failed to upsert cwv_metric for %s/%s: %s", url, device, exc)
                counters["fetch_errors"] += 1
                continue

            # 2. Below-threshold check
            if perf is not None and perf < THRESHOLDS[device]:
                if open_issue(
                    cur, url=url, issue_type="cwv_below_threshold",
                    severity=ISSUE_SEVERITY["cwv_below_threshold"],
                    device=device,
                    details={"score": perf, "threshold": THRESHOLDS[device], "source": r.get("source")},
                ):
                    counters["below_threshold_open"] += 1
                current_open.add((url, "cwv_below_threshold", device))

            # 3. Regression check
            regs = compute_regressions(previous.get((url, device)), r)
            if regs:
                if open_issue(
                    cur, url=url, issue_type="cwv_regression",
                    severity=ISSUE_SEVERITY["cwv_regression"],
                    device=device,
                    details={"regressions": regs, "source": r.get("source")},
                ):
                    counters["regression_open"] += 1
                current_open.add((url, "cwv_regression", device))

            conn.commit()  # commit per result for safety

        # 4. Resolve stale issues
        counters["issues_resolved"] = resolve_stale_issues(
            cur,
            current_open=current_open,
            issue_types=list(ISSUE_SEVERITY.keys()),
            urls_checked=urls_checked,
        )
        conn.commit()

    return counters


# ---------------------------------------------------------------------------
# Console summary
# ---------------------------------------------------------------------------

def print_summary(work_items: list[tuple[str, str]], counters: dict,
                  results: list[dict], duration: float, dry_run: bool) -> None:
    print()
    print(f"  {'=' * 72}")
    print(f"   CWV MONITOR — {date.today().isoformat()}{'  [DRY RUN]' if dry_run else ''}")
    print(f"  {'=' * 72}")
    print()
    print(f"  Work items:           {len(work_items)}")
    print(f"  Fetched OK:           {len(results) - counters['fetch_errors']}")
    print(f"  Fetch errors:         {counters['fetch_errors']}")
    print(f"  Metrics written:      {counters['metrics_written']}")
    print(f"  Below-threshold open: {counters['below_threshold_open']}")
    print(f"  Regressions open:     {counters['regression_open']}")
    print(f"  Issues resolved:      {counters['issues_resolved']}")
    print(f"  Duration:             {duration:.1f}s")
    print()

    # Surface low-scoring pages so the user sees the most actionable items
    low = [
        r for r in results
        if r.get("performance_score") is not None
        and r["performance_score"] < THRESHOLDS[r["strategy"]]
    ]
    if low:
        low.sort(key=lambda r: r["performance_score"] or 0)
        print(f"  Lowest-scoring pages (top {min(10, len(low))}):")
        for r in low[:10]:
            print(f"    {r['strategy']:<8}  score {r['performance_score']:>3}  "
                  f"(thr {THRESHOLDS[r['strategy']]})  {r['url']}")
        print()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(domain: str | None = None,
        page_types: tuple[str, ...] = DEFAULT_PAGE_TYPES,
        strategies: tuple[str, ...] = DEFAULT_STRATEGIES,
        cadence_days: int = DEFAULT_CADENCE_DAYS,
        workers: int = DEFAULT_WORKERS,
        force_all: bool = False,
        dry_run: bool = False) -> dict:
    start = time.monotonic()
    run_date = date.today()

    urls = load_pages(domain, page_types)
    if not urls:
        msg = "No pages match the filter"
        if domain: msg += f" (domain={domain})"
        msg += f" (page_types={list(page_types)})"
        logger.warning("%s", msg)
        return {"status": "skipped", "reason": "no pages"}

    work_items = (
        [(u, s) for u in urls for s in strategies]
        if force_all
        else filter_due_work_items(urls, strategies, cadence_days)
    )
    if not work_items:
        logger.info("No work items due (cadence_days=%d). Use --all to force.", cadence_days)
        return {"status": "skipped", "reason": "all up to date"}

    logger.info("Fetching %d (url, strategy) pairs across %d unique URLs (workers=%d)",
                len(work_items), len({u for u, _ in work_items}), workers)

    previous = load_previous_metrics(work_items) if not force_all else {}
    results = fetch_all_metrics(work_items, workers)

    counters = write_run_results(results, previous, run_date, dry_run)
    duration = time.monotonic() - start

    if not dry_run:
        record_agent_run(
            agent_name=AGENT_NAME,
            status="success" if counters["fetch_errors"] == 0 else "partial",
            records_processed=counters["metrics_written"],
            errors=[r["error"] for r in results if r.get("error")][:25],
            duration_seconds=round(duration, 2),
            metadata={
                "run_date":             run_date.isoformat(),
                "domain":               domain,
                "page_types":           list(page_types),
                "strategies":           list(strategies),
                "cadence_days":         cadence_days,
                "force_all":            force_all,
                "work_items":           len(work_items),
                "urls_checked":         len({u for u, _ in work_items}),
                "metrics_written":      counters["metrics_written"],
                "below_threshold_open": counters["below_threshold_open"],
                "regression_open":      counters["regression_open"],
                "issues_resolved":      counters["issues_resolved"],
                "fetch_errors":         counters["fetch_errors"],
            },
        )

    print_summary(work_items, counters, results, duration, dry_run)
    return {
        "status":   "success" if counters["fetch_errors"] == 0 else "partial",
        "run_date": run_date.isoformat(),
        "counters": counters,
        "duration_seconds": round(duration, 2),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Damco CWV Monitor")
    parser.add_argument("--domain", help="Restrict to one domain")
    parser.add_argument("--page-types", default=",".join(DEFAULT_PAGE_TYPES),
                        help=f"Comma-separated page types (default: {','.join(DEFAULT_PAGE_TYPES)})")
    parser.add_argument("--strategies", default=",".join(DEFAULT_STRATEGIES),
                        help="Comma-separated strategies (mobile,desktop). Default: both")
    parser.add_argument("--cadence", type=int, default=DEFAULT_CADENCE_DAYS,
                        help=f"Per-(url,device) days between checks (default: {DEFAULT_CADENCE_DAYS})")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS,
                        help=f"Parallel workers (default: {DEFAULT_WORKERS})")
    parser.add_argument("--all", dest="force_all", action="store_true",
                        help="Force re-check, ignore cadence")
    parser.add_argument("--dry-run", action="store_true",
                        help="Fetch metrics but don't write to DB")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s  %(name)s  %(levelname)s  %(message)s",
    )

    page_types = tuple(p.strip() for p in args.page_types.split(",") if p.strip())
    strategies = tuple(s.strip() for s in args.strategies.split(",") if s.strip())
    bad_strategies = [s for s in strategies if s not in ("mobile", "desktop")]
    if bad_strategies:
        parser.error(f"Invalid strategies: {bad_strategies}. Valid: mobile, desktop")

    run(domain=args.domain, page_types=page_types, strategies=strategies,
        cadence_days=args.cadence, workers=args.workers,
        force_all=args.force_all, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
