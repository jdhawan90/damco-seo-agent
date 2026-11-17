"""
Pages backfiller — two-phase repair of the pages table.

Why it exists
-------------
Rows in `pages` are created from sitemap URLs only; title, meta_description,
word_count and offering are hydrated by whichever agent touches the row next.
Today that is almost nobody, so 1,055 of 1,058 blog rows have offering=NULL
and the `blog_to_money_page` detector in the internal linker starves.

What it does
------------
Phase A — `hydrate`: fetch every page with NULL title (or last_audited older
than REFRESH_DAYS) and fill title, meta_description, canonical_url, lang,
word_count, last_audited from the crawl.

Phase B — `classify`: for every page where offering IS NULL, score the
hydrated URL path + title + meta_description against every tenant offering's
token vocabulary. Rules for assigning the winning offering:

    - top offering must score >= MIN_SCORE
    - top offering must lead the runner-up by >= MIN_LEAD
    - URL matches are worth URL_WEIGHT per token (highest)
    - Title matches worth TITLE_WEIGHT (second)
    - Meta matches worth META_WEIGHT (third)

Tokens are matched against each text with a word-boundary regex so short
acronyms like "ai" do not sneak into "maintain" or "sustain".

Safety contract
---------------
* Only touches rows where the column being written is NULL (hydrate) or
  the offering is NULL (classify). Writer-set values are never overridden.
* Every classification attempt — even the ones that end up as 'ambiguous'
  or 'no_match' — writes one row to offering_backfill_log so decisions
  are reproducible later.
* --dry-run runs the full classifier and logs decision='dry_run' but
  does not update pages.offering.
* Hydration is idempotent: a row with last_audited within REFRESH_DAYS is
  skipped, so a weekly cron becomes cheap after the first full pass.

CLI
---
    python -m content_operations.pages_backfiller hydrate
    python -m content_operations.pages_backfiller classify [--dry-run]
    python -m content_operations.pages_backfiller run        # both phases
    python -m content_operations.pages_backfiller stats      # summary query
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Iterable
from urllib.parse import urlparse

from common.connectors.crawler import Crawler, CrawlResult
from common.database import connection, fetch_all, record_agent_run
from common.tenant import profile

logger = logging.getLogger("content_operations.pages_backfiller")

AGENT_NAME = "content_operations.pages_backfiller"

# --- hydration -------------------------------------------------------------
REFRESH_DAYS = 90        # skip rows audited within this window
HYDRATE_RATE_LIMIT = 1.0  # polite default

# --- classification scoring ------------------------------------------------
URL_WEIGHT   = 3  # URL hit is a strong signal — the path encoded the topic deliberately
TITLE_WEIGHT = 2  # Title tokens almost always reflect the topic
META_WEIGHT  = 2  # Meta description is close to title in reliability
MIN_SCORE    = 3  # Classifier must reach this absolute floor to assign
MIN_LEAD     = 2  # Must beat runner-up by this many points — otherwise ambiguous


# ---------------------------------------------------------------------------
# Token matching
# ---------------------------------------------------------------------------

def _compile_matchers() -> dict[str, list[tuple[str, re.Pattern[str]]]]:
    """
    Build {offering_name: [(token_literal, word-boundary regex), ...]}.

    Tokens come from tenant_offerings.tokens AND niche_tokens — both lists are
    tuned by the SEO team for this exact purpose, and treating them at the
    same weight keeps the scorer simple to reason about.

    Word-boundary matching is critical: a substring test would match "ai"
    inside "maintain", "aim", "chain" — 356 blog URLs appeared to mention
    "ai" by substring, most of them false positives.
    """
    p = profile()
    out: dict[str, list[tuple[str, re.Pattern[str]]]] = {}
    for off in p.offerings:
        pats: list[tuple[str, re.Pattern[str]]] = []
        for raw in list(off.tokens) + list(off.niche_tokens):
            tok = (raw or "").strip().lower()
            if not tok:
                continue
            # Escape, then replace any whitespace run with \W+ so "artificial
            # intelligence" matches "artificial  intelligence" or
            # "artificial-intelligence" or "artificial_intelligence".
            parts = [re.escape(part) for part in tok.split() if part]
            if not parts:
                continue
            pattern = r"\b" + r"\W+".join(parts) + r"\b"
            pats.append((tok, re.compile(pattern, re.IGNORECASE)))
        if pats:
            out[off.name] = pats
    return out


def _tokens_matched(text: str, matchers: list[tuple[str, re.Pattern[str]]]) -> list[str]:
    """Return every token (literal form) whose pattern matches anywhere in text."""
    if not text:
        return []
    hits: list[str] = []
    for literal, pat in matchers:
        if pat.search(text):
            hits.append(literal)
    return hits


# ---------------------------------------------------------------------------
# Phase A — hydrate
# ---------------------------------------------------------------------------

@dataclass
class HydrateStats:
    attempted:    int = 0
    fetched:      int = 0
    updated:      int = 0
    skipped:      int = 0
    errors:       int = 0
    by_status:    Counter = field(default_factory=Counter)


def _rows_to_hydrate(page_type: str | None, max_pages: int | None,
                    force: bool) -> list[dict]:
    """
    Rows that need hydration. A row qualifies when title IS NULL, or when
    `force` is set and last_audited is older than REFRESH_DAYS.
    """
    sql = """
        SELECT id, url
          FROM pages
         WHERE (title IS NULL OR title = '')
    """
    params: list = []
    if page_type:
        sql += " AND page_type = %s"
        params.append(page_type)
    if force:
        sql += " OR (last_audited IS NULL OR last_audited < CURRENT_TIMESTAMP - INTERVAL '%s days')"
        params.append(REFRESH_DAYS)
    sql += " ORDER BY id"
    if max_pages:
        sql += " LIMIT %s"
        params.append(max_pages)
    return fetch_all(sql, params)


UPDATE_PAGE_HYDRATE = """
    UPDATE pages SET
        title            = coalesce(pages.title, %s),
        meta_description = coalesce(pages.meta_description, %s),
        canonical_url    = coalesce(pages.canonical_url, %s),
        lang             = coalesce(pages.lang, %s),
        word_count       = coalesce(pages.word_count, %s),
        last_audited     = CURRENT_TIMESTAMP,
        updated_at       = CURRENT_TIMESTAMP
     WHERE id = %s
"""


def hydrate(*, page_type: str | None = None,
            max_pages: int | None = None,
            rate_limit_sec: float = HYDRATE_RATE_LIMIT,
            respect_robots: bool = True,
            force: bool = False) -> dict:
    """Fetch pages with missing title and persist title/meta/canonical/lang/word_count."""
    start = time.monotonic()
    stats = HydrateStats()
    api_errors: list[str] = []

    targets = _rows_to_hydrate(page_type, max_pages, force)
    stats.attempted = len(targets)
    logger.info("hydrate: %d pages queued (page_type=%s, force=%s, max=%s)",
                stats.attempted, page_type, force, max_pages)

    p = profile()
    crawler = Crawler(
        user_agent=p.user_agent,
        timeout_sec=20,
        rate_limit_sec=rate_limit_sec,
        respect_robots=respect_robots,
    )

    with connection() as conn:
        cur = conn.cursor()
        for i, row in enumerate(targets, 1):
            try:
                res: CrawlResult = crawler.fetch(row["url"])
            except Exception as exc:
                stats.errors += 1
                api_errors.append(f"{row['url']}: {exc}")
                continue
            stats.by_status[res.status or 0] += 1
            if not res.is_html or res.error:
                stats.skipped += 1
                continue
            stats.fetched += 1
            try:
                cur.execute(
                    UPDATE_PAGE_HYDRATE,
                    (
                        res.title,
                        res.meta_description,
                        res.canonical,
                        res.lang,
                        res.word_count,
                        row["id"],
                    ),
                )
                stats.updated += 1
                if i % 50 == 0:
                    conn.commit()
                    logger.info("hydrate progress: %d/%d, %d updated",
                                i, stats.attempted, stats.updated)
            except Exception as exc:
                conn.rollback()
                stats.errors += 1
                api_errors.append(f"{row['url']}: persist failed: {exc}")
                logger.exception("persist failed for %s", row["url"])
        conn.commit()

    duration = time.monotonic() - start
    record_agent_run(
        agent_name=f"{AGENT_NAME}.hydrate",
        status="success" if not api_errors else "partial",
        records_processed=stats.updated,
        errors=api_errors or None,
        duration_seconds=round(duration, 2),
        metadata={
            "run_date":    date.today().isoformat(),
            "page_type":   page_type,
            "attempted":   stats.attempted,
            "fetched":     stats.fetched,
            "updated":     stats.updated,
            "skipped":     stats.skipped,
            "errors":      stats.errors,
            "status_codes": dict(stats.by_status),
            "force":       force,
        },
    )
    logger.info(
        "hydrate done in %.1fs: %d/%d pages fetched, %d updated, %d errors",
        duration, stats.fetched, stats.attempted, stats.updated, stats.errors,
    )
    return {
        "status": "success" if not api_errors else "partial",
        "attempted": stats.attempted,
        "fetched":   stats.fetched,
        "updated":   stats.updated,
        "errors":    stats.errors,
        "duration_seconds": round(duration, 2),
    }


# ---------------------------------------------------------------------------
# Phase B — classify
# ---------------------------------------------------------------------------

@dataclass
class ClassifyStats:
    seen:        int = 0
    assigned:    int = 0
    ambiguous:   int = 0
    no_match:    int = 0
    skipped:     int = 0
    by_offering: Counter = field(default_factory=Counter)


def _score_page(page: dict,
                matchers_by_offering: dict[str, list[tuple[str, re.Pattern[str]]]]
                ) -> tuple[dict[str, int],
                           dict[str, list[str]], dict[str, list[str]],
                           dict[str, list[str]]]:
    """
    Return (scores, url_hits, title_hits, meta_hits).

    URL text is the path segment lowercased and spaces/dashes/underscores
    normalised to spaces — otherwise a slug like "salesforce-crm-tips" would
    only match "salesforce-crm-tips" literally, never "salesforce".
    """
    url_text = _url_text(page["url"])
    title_text = (page.get("title") or "")
    meta_text  = (page.get("meta_description") or "")

    scores: dict[str, int] = {}
    url_hits:   dict[str, list[str]] = {}
    title_hits: dict[str, list[str]] = {}
    meta_hits:  dict[str, list[str]] = {}

    for offering_name, pats in matchers_by_offering.items():
        u = _tokens_matched(url_text, pats)
        t = _tokens_matched(title_text, pats)
        m = _tokens_matched(meta_text, pats)
        if not (u or t or m):
            continue
        score = (len(u) * URL_WEIGHT
                 + len(t) * TITLE_WEIGHT
                 + len(m) * META_WEIGHT)
        scores[offering_name] = score
        url_hits[offering_name]   = u
        title_hits[offering_name] = t
        meta_hits[offering_name]  = m
    return scores, url_hits, title_hits, meta_hits


def _url_text(url: str) -> str:
    """Path, lowercased, slug separators normalised to spaces."""
    try:
        path = urlparse(url).path or ""
    except Exception:
        path = url
    path = path.lower().replace("-", " ").replace("_", " ").replace("/", " ")
    return re.sub(r"\s+", " ", path).strip()


def _decide(scores: dict[str, int]) -> tuple[str, str | None, int,
                                             str | None, int, float]:
    """
    Classifier decision. Returns:
      (decision, top_offering, top_score, runner_up, runner_up_score, confidence)
    where decision is one of 'assigned', 'ambiguous', 'no_match'.
    """
    if not scores:
        return "no_match", None, 0, None, 0, 0.0
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    top, top_score = ranked[0]
    runner, runner_score = (ranked[1] if len(ranked) >= 2 else (None, 0))
    if top_score < MIN_SCORE:
        return "no_match", top, top_score, runner, runner_score, 0.0
    lead = top_score - runner_score
    if lead < MIN_LEAD:
        return "ambiguous", top, top_score, runner, runner_score, 0.0
    # Confidence: lead over the next offering normalised against top score.
    # Capped at 100 for display; 70+ is "very confident".
    confidence = min(100.0, round(100.0 * lead / max(top_score, 1), 2))
    return "assigned", top, top_score, runner, runner_score, confidence


INSERT_LOG_SQL = """
    INSERT INTO offering_backfill_log
        (page_id, decision, assigned_offering, confidence_score,
         top_offering, top_score, runner_up_offering, runner_up_score,
         url_matches, title_matches, meta_matches)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""

UPDATE_PAGE_OFFERING = """
    UPDATE pages SET offering = %s, updated_at = CURRENT_TIMESTAMP
     WHERE id = %s AND offering IS NULL
"""


def classify(*, page_type: str | None = None,
             dry_run: bool = False,
             max_pages: int | None = None,
             min_lead: int = MIN_LEAD,
             min_score: int = MIN_SCORE) -> dict:
    """
    Classify every page where offering IS NULL. Writes pages.offering in place
    (unless --dry-run) and a log row per attempt.
    """
    start = time.monotonic()
    stats = ClassifyStats()

    # Pull candidates.
    sql = """
        SELECT id, url, title, meta_description, page_type
          FROM pages
         WHERE offering IS NULL
    """
    params: list = []
    if page_type:
        sql += " AND page_type = %s"
        params.append(page_type)
    sql += " ORDER BY id"
    if max_pages:
        sql += " LIMIT %s"
        params.append(max_pages)
    candidates = fetch_all(sql, params)
    stats.seen = len(candidates)
    logger.info("classify: %d candidates (page_type=%s, dry_run=%s)",
                stats.seen, page_type, dry_run)

    matchers_by_offering = _compile_matchers()
    logger.info("classifier vocabulary: %d offerings, "
                "%d patterns total",
                len(matchers_by_offering),
                sum(len(v) for v in matchers_by_offering.values()))

    with connection() as conn:
        cur = conn.cursor()
        for page in candidates:
            scores, u, t, m = _score_page(page, matchers_by_offering)
            decision, top, top_s, runner, runner_s, conf = _decide(scores)

            # Count the decision
            if decision == "assigned":
                stats.assigned += 1
                stats.by_offering[top] += 1
            elif decision == "ambiguous":
                stats.ambiguous += 1
            else:
                stats.no_match += 1

            # Audit-log every attempt.
            logged_decision = "dry_run" if dry_run else decision
            cur.execute(INSERT_LOG_SQL, (
                page["id"],
                logged_decision,
                top if decision == "assigned" and not dry_run else None,
                conf if decision == "assigned" else None,
                top, top_s, runner, runner_s,
                json.dumps(u), json.dumps(t), json.dumps(m),
            ))

            # Only write pages.offering on a real assignment.
            if decision == "assigned" and not dry_run:
                cur.execute(UPDATE_PAGE_OFFERING, (top, page["id"]))

        conn.commit()

    duration = time.monotonic() - start
    record_agent_run(
        agent_name=f"{AGENT_NAME}.classify",
        status="success",
        records_processed=stats.assigned,
        errors=None,
        duration_seconds=round(duration, 2),
        metadata={
            "run_date":    date.today().isoformat(),
            "page_type":   page_type,
            "dry_run":     dry_run,
            "seen":        stats.seen,
            "assigned":    stats.assigned,
            "ambiguous":   stats.ambiguous,
            "no_match":    stats.no_match,
            "by_offering": dict(stats.by_offering),
            "min_score":   min_score,
            "min_lead":    min_lead,
        },
    )
    logger.info(
        "classify done in %.1fs: assigned=%d  ambiguous=%d  no_match=%d (of %d)%s",
        duration, stats.assigned, stats.ambiguous, stats.no_match, stats.seen,
        " [dry-run; no writes]" if dry_run else "",
    )
    return {
        "status":    "success",
        "seen":      stats.seen,
        "assigned":  stats.assigned,
        "ambiguous": stats.ambiguous,
        "no_match":  stats.no_match,
        "by_offering": dict(stats.by_offering),
        "dry_run":   dry_run,
        "duration_seconds": round(duration, 2),
    }


# ---------------------------------------------------------------------------
# Quick stats query (for --stats and for other agents)
# ---------------------------------------------------------------------------

def stats_report() -> dict:
    """Snapshot of the backfill state, for print or JSON."""
    by_type = fetch_all(
        """
        SELECT page_type,
               count(*) AS total,
               count(*) FILTER (WHERE offering IS NOT NULL) AS with_offering,
               count(*) FILTER (WHERE title IS NOT NULL AND title <> '') AS with_title
          FROM pages
         GROUP BY page_type ORDER BY page_type NULLS LAST
        """
    )
    recent = fetch_all(
        """
        SELECT decision, count(*) n
          FROM offering_backfill_log
         WHERE run_date > CURRENT_TIMESTAMP - INTERVAL '30 days'
         GROUP BY 1 ORDER BY 2 DESC
        """
    )
    return {"pages_by_type": by_type, "recent_decisions_30d": recent}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description=f"{profile().brand_name} Pages Backfiller — hydrate + classify",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    ph = sub.add_parser("hydrate", help="fetch pages and fill title/meta/canonical")
    ph.add_argument("--page-type", default=None, help="restrict to one page_type (blog, service, …)")
    ph.add_argument("--max-pages", type=int, default=None)
    ph.add_argument("--rate-limit", type=float, default=HYDRATE_RATE_LIMIT)
    ph.add_argument("--no-robots", action="store_true")
    ph.add_argument("--force", action="store_true",
                    help="also refresh rows last_audited > %d days" % REFRESH_DAYS)
    ph.add_argument("-v", "--verbose", action="store_true")

    pc = sub.add_parser("classify", help="score URL+title+meta against offering vocabulary")
    pc.add_argument("--page-type", default=None)
    pc.add_argument("--max-pages", type=int, default=None)
    pc.add_argument("--dry-run", action="store_true", help="log decisions without writing pages.offering")
    pc.add_argument("--min-score", type=int, default=MIN_SCORE)
    pc.add_argument("--min-lead",  type=int, default=MIN_LEAD)
    pc.add_argument("-v", "--verbose", action="store_true")

    pr = sub.add_parser("run", help="hydrate then classify")
    pr.add_argument("--page-type", default=None)
    pr.add_argument("--max-pages", type=int, default=None)
    pr.add_argument("--rate-limit", type=float, default=HYDRATE_RATE_LIMIT)
    pr.add_argument("--no-robots", action="store_true")
    pr.add_argument("--force", action="store_true")
    pr.add_argument("--dry-run", action="store_true")
    pr.add_argument("-v", "--verbose", action="store_true")

    sub.add_parser("stats", help="show pages coverage + recent decisions")

    args = parser.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if getattr(args, "verbose", False) else logging.INFO,
        format="%(asctime)s  %(name)s  %(levelname)s  %(message)s",
    )

    if args.cmd == "hydrate":
        out = hydrate(
            page_type=args.page_type,
            max_pages=args.max_pages,
            rate_limit_sec=args.rate_limit,
            respect_robots=not args.no_robots,
            force=args.force,
        )
        print(f"hydrate: {out}")
        return

    if args.cmd == "classify":
        out = classify(
            page_type=args.page_type,
            dry_run=args.dry_run,
            max_pages=args.max_pages,
            min_lead=args.min_lead,
            min_score=args.min_score,
        )
        print(f"classify: {out}")
        return

    if args.cmd == "run":
        print("=" * 72)
        print("1/2 HYDRATE")
        print("=" * 72)
        h = hydrate(
            page_type=args.page_type,
            max_pages=args.max_pages,
            rate_limit_sec=args.rate_limit,
            respect_robots=not args.no_robots,
            force=args.force,
        )
        print(f"  {h}")
        print()
        print("=" * 72)
        print("2/2 CLASSIFY")
        print("=" * 72)
        c = classify(
            page_type=args.page_type,
            dry_run=args.dry_run,
        )
        print(f"  {c}")
        return

    if args.cmd == "stats":
        s = stats_report()
        print("pages_by_type:")
        for r in s["pages_by_type"]:
            print(f"  {str(r['page_type']):<10} total={r['total']:>5}  "
                  f"with_offering={r['with_offering']:>5}  with_title={r['with_title']:>5}")
        print()
        print("recent_decisions_30d:")
        for r in s["recent_decisions_30d"]:
            print(f"  {r['decision']:<20} {r['n']}")
        return


if __name__ == "__main__":
    main()
