"""
Internal Link Analyzer — Phase 5 of Technical SEO agent
========================================================

Standard agent lifecycle:
  Read    — fetch pages from `pages` table (filtered by domain/page_type)
  Process — crawl each page, extract internal links, build the directed
            link graph; compute PageRank-style equity flow; find orphans,
            dead-ends, and under-linked priority pages
  Write   — UPSERT graph edges into `internal_links`; open/resolve
            findings in `technical_issues`; persist a narrative report
            under outputs/audits/; log `agent_runs`
  Notify  — console summary with top-N rankings + counts

Issue types emitted
-------------------
  orphan_page         (medium) — page with 0 incoming internal links
  dead_end_page       (low)    — page with 0 outgoing internal links
  underlinked_pillar  (high)   — pillar page with <5 inbound internal links
  underlinked_service (medium) — service page with <3 inbound internal links

LLM-assisted anchor-text recommendations are intentionally deferred to v2.
Rule-based findings + a list of high-PageRank source-page candidates are
already actionable; the narrative report surfaces these as
"these top X high-equity pages don't link to your under-linked target Y".

Usage
-----
    # Default: all 3 domains, default page_types, weekly cadence
    python -m technical_seo.internal_link_analyzer

    # One domain
    python -m technical_seo.internal_link_analyzer --domain damcogroup.com

    # Larger scope — include blog/resource as graph nodes too
    python -m technical_seo.internal_link_analyzer --page-types home,pillar,service,blog,resource

    # Force re-crawl ignoring cadence (also clears internal_links rows older
    # than this run's date_crawled for the scope)
    python -m technical_seo.internal_link_analyzer --all

    # Analyze the existing graph without re-crawling (useful for iteration
    # on detection/scoring after a previous build)
    python -m technical_seo.internal_link_analyzer --skip-crawl

    # Dry run — fetch + analyze but don't write
    python -m technical_seo.internal_link_analyzer --dry-run
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import urlparse, urlunparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from common.connectors.crawler import Crawler, CrawlResult
from common.database import connection, fetch_all, record_agent_run


logger = logging.getLogger("internal_link_analyzer")

AGENT_NAME = "technical_seo.internal_link_analyzer"

DEFAULT_PAGE_TYPES = ("home", "pillar", "service")
LARGE_SCOPE_PAGE_TYPES = ("home", "pillar", "service", "blog", "resource")
DEFAULT_CADENCE_DAYS = 14   # links don't change as fast as content; check fortnightly
DEFAULT_WORKERS = 4

# PageRank settings
PR_DAMPING = 0.85
PR_MAX_ITERATIONS = 50
PR_TOLERANCE = 1e-6

# Inbound-link thresholds by page_type. Pages below are flagged.
INBOUND_THRESHOLDS = {
    "pillar":  5,
    "service": 3,
}

SEVERITY = {
    "orphan_page":         "medium",
    "dead_end_page":       "low",
    "underlinked_pillar":  "high",
    "underlinked_service": "medium",
}

OUTPUT_DIR = Path(__file__).resolve().parent.parent / "outputs" / "audits"


# ---------------------------------------------------------------------------
# URL normalization
# ---------------------------------------------------------------------------

def normalize_url(url: str) -> str:
    """
    Light normalization so /page and /page/ are treated as the same target.
    - lowercase scheme + host
    - strip URL fragment
    - strip trailing slash except for root path
    - drop default ports (:80, :443)
    Path case is preserved (some servers ARE case-sensitive on path).
    """
    if not url:
        return url
    p = urlparse(url)
    scheme = (p.scheme or "https").lower()
    netloc = (p.hostname or "").lower()
    if p.port and not (
        (scheme == "http" and p.port == 80) or (scheme == "https" and p.port == 443)
    ):
        netloc += f":{p.port}"
    path = p.path or "/"
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")
    return urlunparse((scheme, netloc, path, p.params, p.query, ""))


def _origin(url: str) -> str | None:
    p = urlparse(url)
    if p.scheme and p.netloc:
        return f"{p.scheme}://{p.netloc}".lower()
    return None


# ---------------------------------------------------------------------------
# Read phase
# ---------------------------------------------------------------------------

def load_pages(domain: str | None, page_types: tuple[str, ...]) -> list[dict]:
    sql = "SELECT url, page_type FROM pages WHERE page_type = ANY(%s)"
    params: list = [list(page_types)]
    if domain:
        sql += " AND url LIKE %s"
        params.append(f"%{domain}%")
    sql += " ORDER BY url"
    return fetch_all(sql, params)


# ---------------------------------------------------------------------------
# Crawl phase
# ---------------------------------------------------------------------------

def crawl_one(crawler: Crawler, url: str) -> tuple[str, CrawlResult]:
    try:
        return url, crawler.fetch(url)
    except Exception as exc:
        cr = CrawlResult(url=url, error=str(exc))
        return url, cr


def crawl_and_collect_links(pages: list[dict], crawler: Crawler,
                            workers: int) -> tuple[list[tuple[str, str, str | None]], dict]:
    """
    Crawl every page in `pages`. Returns:
        edges: list of (source_url, target_url, anchor_text)  — internal only
        stats: counters
    """
    edges: list[tuple[str, str, str | None]] = []
    stats = {"fetched_ok": 0, "fetch_errors": 0, "pages_with_no_links": 0}
    page_origins = {_origin(p["url"]) for p in pages}

    if not pages:
        return edges, stats

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(crawl_one, crawler, p["url"]): p for p in pages}
        for i, fut in enumerate(as_completed(futures), 1):
            page = futures[fut]
            if i % 25 == 0 or i == len(pages):
                logger.info("  crawled %d/%d", i, len(pages))
            try:
                url, r = fut.result()
            except Exception as exc:
                logger.error("crawler.fetch failed for %s: %s", page["url"], exc)
                stats["fetch_errors"] += 1
                continue
            if r.error or not r.is_html:
                stats["fetch_errors"] += 1
                continue
            stats["fetched_ok"] += 1

            # Filter to internal links and dedupe (source, target, anchor)
            page_internal = 0
            seen_edges: set[tuple[str, str, str | None]] = set()
            for link in r.links:
                if not link.get("is_internal"):
                    continue
                href = link.get("href")
                if not href:
                    continue
                target_norm = normalize_url(href)
                # Cross-property links (damcogroup.com → achieva.ai) are marked
                # is_internal=False by the crawler (different origin). Internal
                # here means same origin as the source page.
                edge = (normalize_url(r.final_url or url), target_norm,
                        (link.get("anchor") or None))
                if edge in seen_edges:
                    continue
                seen_edges.add(edge)
                edges.append(edge)
                page_internal += 1

            if page_internal == 0:
                stats["pages_with_no_links"] += 1

    return edges, stats


def upsert_edges(edges: list[tuple[str, str, str | None]], dry_run: bool) -> int:
    """Insert edges with ON CONFLICT DO NOTHING (UNIQUE handles dedupe)."""
    if dry_run or not edges:
        return 0
    inserted = 0
    with connection() as conn:
        with conn.cursor() as cur:
            for src, tgt, anchor in edges:
                cur.execute(
                    """
                    INSERT INTO internal_links (source_url, target_url, anchor_text)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (source_url, target_url, anchor_text) DO NOTHING
                    """,
                    (src, tgt, anchor),
                )
                if cur.rowcount > 0:
                    inserted += 1
    return inserted


def load_existing_edges(domain: str | None) -> list[tuple[str, str]]:
    """Read current graph from internal_links (used in --skip-crawl mode)."""
    sql = "SELECT source_url, target_url FROM internal_links"
    params: list = []
    if domain:
        sql += " WHERE source_url LIKE %s"
        params.append(f"%{domain}%")
    rows = fetch_all(sql, params)
    return [(r["source_url"], r["target_url"]) for r in rows]


# ---------------------------------------------------------------------------
# Analysis phase
# ---------------------------------------------------------------------------

def compute_pagerank(edges: list[tuple[str, str]],
                     damping: float = PR_DAMPING,
                     iterations: int = PR_MAX_ITERATIONS,
                     tol: float = PR_TOLERANCE) -> dict[str, float]:
    """Simple iterative PageRank. Returns {url: score}."""
    nodes: set[str] = set()
    out_edges: dict[str, list[str]] = defaultdict(list)
    in_edges: dict[str, list[str]] = defaultdict(list)

    for src, tgt in edges:
        nodes.add(src); nodes.add(tgt)
        out_edges[src].append(tgt)
        in_edges[tgt].append(src)

    n = len(nodes)
    if n == 0:
        return {}

    pr = {node: 1.0 / n for node in nodes}
    base = (1 - damping) / n

    for _ in range(iterations):
        new_pr: dict[str, float] = {}
        # Distribute mass from dangling nodes (no outbound) uniformly
        dangling_mass = sum(pr[node] for node in nodes if not out_edges[node])
        for node in nodes:
            score = base + damping * dangling_mass / n
            for src in in_edges[node]:
                out_count = len(out_edges[src])
                if out_count > 0:
                    score += damping * pr[src] / out_count
            new_pr[node] = score
        delta = sum(abs(new_pr[node] - pr[node]) for node in nodes)
        pr = new_pr
        if delta < tol:
            break

    return pr


def find_orphans(edges: list[tuple[str, str]], page_urls: set[str]) -> list[str]:
    """Pages in our scope with 0 incoming internal links."""
    in_set = {tgt for _, tgt in edges}
    return sorted(p for p in page_urls if p not in in_set)


def find_dead_ends(edges: list[tuple[str, str]], page_urls: set[str]) -> list[str]:
    """Pages in our scope with 0 outgoing internal links."""
    out_set = {src for src, _ in edges}
    return sorted(p for p in page_urls if p not in out_set)


def find_underlinked(edges: list[tuple[str, str]],
                     pages: list[dict]) -> list[dict]:
    """Priority page_types whose inbound count is below threshold."""
    inbound_counts = Counter(tgt for _, tgt in edges)
    out: list[dict] = []
    for p in pages:
        pt = p["page_type"]
        threshold = INBOUND_THRESHOLDS.get(pt)
        if threshold is None:
            continue
        normalized = normalize_url(p["url"])
        count = inbound_counts.get(normalized, 0)
        if count < threshold:
            out.append({
                "url":       p["url"],
                "page_type": pt,
                "inbound":   count,
                "threshold": threshold,
            })
    return sorted(out, key=lambda r: (r["inbound"], r["url"]))


def recommend_sources(target_url: str, edges: list[tuple[str, str]],
                      pagerank: dict[str, float], n: int = 5) -> list[dict]:
    """
    For an under-linked target, suggest the top-N high-PageRank source pages
    that *don't* currently link to it. Same origin only.
    """
    target_norm = normalize_url(target_url)
    target_origin = _origin(target_norm)
    existing_sources = {src for src, tgt in edges if tgt == target_norm}

    candidates = [
        (url, pr) for url, pr in pagerank.items()
        if url != target_norm
        and url not in existing_sources
        and _origin(url) == target_origin
    ]
    candidates.sort(key=lambda x: -x[1])
    return [{"source": url, "pagerank": round(pr, 6)} for url, pr in candidates[:n]]


# ---------------------------------------------------------------------------
# Write phase: technical_issues
# ---------------------------------------------------------------------------

def find_open_issue(cur, url: str, issue_type: str) -> int | None:
    cur.execute(
        """
        SELECT id FROM technical_issues
         WHERE url = %s AND issue_type = %s AND date_resolved IS NULL
         LIMIT 1
        """,
        (url, issue_type),
    )
    row = cur.fetchone()
    if not row:
        return None
    return row[0] if not isinstance(row, dict) else row["id"]


def open_or_update_issue(cur, url: str, issue_type: str,
                         severity: str, details: dict) -> str:
    existing = find_open_issue(cur, url, issue_type)
    if existing is None:
        cur.execute(
            """
            INSERT INTO technical_issues (url, issue_type, severity, details)
            VALUES (%s, %s, %s, %s::jsonb)
            """,
            (url, issue_type, severity, json.dumps(details)),
        )
        return "inserted"
    cur.execute(
        "UPDATE technical_issues SET details = %s::jsonb WHERE id = %s",
        (json.dumps(details), existing),
    )
    return "updated"


def resolve_stale_issues(cur, urls_analyzed: set[str],
                         current_open: set[tuple[str, str]]) -> int:
    if not urls_analyzed:
        return 0
    cur.execute(
        """
        SELECT id, url, issue_type
          FROM technical_issues
         WHERE date_resolved IS NULL
           AND issue_type = ANY(%s)
           AND url = ANY(%s)
        """,
        (list(SEVERITY.keys()), list(urls_analyzed)),
    )
    resolved = 0
    for row in cur.fetchall():
        rid   = row[0] if not isinstance(row, dict) else row["id"]
        url   = row[1] if not isinstance(row, dict) else row["url"]
        itype = row[2] if not isinstance(row, dict) else row["issue_type"]
        if (url, itype) in current_open:
            continue
        cur.execute("UPDATE technical_issues SET date_resolved = now() WHERE id = %s", (rid,))
        resolved += 1
    return resolved


def write_findings(*, pages: list[dict], edges: list[tuple[str, str]],
                   orphans: list[str], dead_ends: list[str],
                   underlinked: list[dict], dry_run: bool) -> dict:
    counters = {"inserted": 0, "updated": 0, "resolved": 0}
    if dry_run:
        return counters

    # Map normalized → original URL so issues are recorded with the canonical
    # spelling from pages.url (consistent with other agents' output).
    norm_to_url = {normalize_url(p["url"]): p["url"] for p in pages}
    priority_types = {p["page_type"] for p in pages if p["page_type"] in ("home", "pillar", "service")}
    page_by_url = {normalize_url(p["url"]): p for p in pages}
    urls_analyzed = {p["url"] for p in pages}
    current_open: set[tuple[str, str]] = set()

    with connection() as conn:
        cur = conn.cursor()

        # Orphans — only flag priority page types
        for norm_url in orphans:
            page = page_by_url.get(norm_url)
            if not page or page["page_type"] not in priority_types:
                continue
            orig = norm_to_url.get(norm_url, norm_url)
            outcome = open_or_update_issue(
                cur, orig, "orphan_page", SEVERITY["orphan_page"],
                {"page_type": page["page_type"]},
            )
            counters[outcome] += 1
            current_open.add((orig, "orphan_page"))

        # Dead-ends — flag any page in scope (low severity; usually intentional
        # for landing pages, but worth noting on pillars/services)
        for norm_url in dead_ends:
            page = page_by_url.get(norm_url)
            if not page:
                continue
            orig = norm_to_url.get(norm_url, norm_url)
            outcome = open_or_update_issue(
                cur, orig, "dead_end_page", SEVERITY["dead_end_page"],
                {"page_type": page["page_type"]},
            )
            counters[outcome] += 1
            current_open.add((orig, "dead_end_page"))

        # Under-linked pillars + services
        for u in underlinked:
            issue_type = "underlinked_pillar" if u["page_type"] == "pillar" else "underlinked_service"
            outcome = open_or_update_issue(
                cur, u["url"], issue_type, SEVERITY[issue_type],
                {"inbound": u["inbound"], "threshold": u["threshold"],
                 "page_type": u["page_type"]},
            )
            counters[outcome] += 1
            current_open.add((u["url"], issue_type))

        counters["resolved"] = resolve_stale_issues(cur, urls_analyzed, current_open)
        conn.commit()

    return counters


# ---------------------------------------------------------------------------
# Report writing
# ---------------------------------------------------------------------------

def write_report(*, run_date: date, domain: str | None,
                 pages: list[dict], edges: list[tuple[str, str]],
                 pagerank: dict[str, float],
                 orphans: list[str], dead_ends: list[str],
                 underlinked: list[dict]) -> Path:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    suffix = f"_{domain.replace('.', '_')}" if domain else ""
    path = OUTPUT_DIR / f"internal_link_report_{run_date.isoformat()}{suffix}.md"

    n_nodes = len({u for u in pagerank})
    n_edges = len(edges)
    avg_out = (n_edges / n_nodes) if n_nodes else 0.0

    # Top-PR and bottom-PR (within page scope)
    scope_norm = {normalize_url(p["url"]) for p in pages}
    in_scope_pr = [(u, pr) for u, pr in pagerank.items() if u in scope_norm]
    in_scope_pr.sort(key=lambda x: -x[1])
    top10 = in_scope_pr[:10]
    bottom10 = sorted(in_scope_pr, key=lambda x: x[1])[:10]

    lines: list[str] = []
    lines.append(f"# Internal Link Audit — {run_date.isoformat()}")
    if domain:
        lines.append(f"\n*Scope: `{domain}`*")
    lines.append("")
    lines.append("## Graph stats")
    lines.append("")
    lines.append(f"- Nodes (pages in graph): **{n_nodes}**")
    lines.append(f"- Edges (internal links): **{n_edges}**")
    lines.append(f"- Average outbound links per page: **{avg_out:.1f}**")
    lines.append(f"- Pages analyzed (scope): **{len(pages)}**")
    lines.append("")

    lines.append("## Top 10 pages by PageRank (in scope)")
    lines.append("")
    lines.append("| Rank | PageRank | URL |")
    lines.append("|---:|---:|:---|")
    for i, (url, pr) in enumerate(top10, 1):
        lines.append(f"| {i} | {pr:.5f} | `{url}` |")
    lines.append("")

    lines.append(f"## Orphan pages — {len(orphans)} total (only priority types flagged as issues)")
    lines.append("")
    if not orphans:
        lines.append("_None._")
    else:
        page_by_norm = {normalize_url(p["url"]): p for p in pages}
        priority_orphans = [u for u in orphans if page_by_norm.get(u) and page_by_norm[u]["page_type"] in ("home", "pillar", "service")]
        lines.append(f"### Priority-type orphans ({len(priority_orphans)})")
        for u in priority_orphans[:30]:
            pt = page_by_norm[u]["page_type"]
            lines.append(f"- ({pt}) `{u}`")
        other_count = len(orphans) - len(priority_orphans)
        if other_count > 0:
            lines.append(f"\n_Plus {other_count} other orphans of non-priority types (not flagged as issues)._")
    lines.append("")

    lines.append(f"## Dead-end pages — {len(dead_ends)} total")
    lines.append("")
    if not dead_ends:
        lines.append("_None._")
    else:
        for u in dead_ends[:30]:
            lines.append(f"- `{u}`")
        if len(dead_ends) > 30:
            lines.append(f"\n_…plus {len(dead_ends) - 30} more (see technical_issues)._")
    lines.append("")

    lines.append(f"## Under-linked priority pages — {len(underlinked)} total")
    lines.append("")
    if not underlinked:
        lines.append("_None — all priority pages meet inbound-link thresholds._")
    else:
        for u in underlinked:
            lines.append(f"### `{u['url']}`")
            lines.append(f"- Type: **{u['page_type']}**")
            lines.append(f"- Inbound links: **{u['inbound']}** (threshold: {u['threshold']})")
            recs = recommend_sources(u["url"], edges, pagerank, n=5)
            if recs:
                lines.append("- Suggested source pages (top high-equity pages that don't currently link here):")
                for r in recs:
                    lines.append(f"  - `{r['source']}` (PageRank {r['pagerank']})")
            else:
                lines.append("- No clear high-equity source candidates found in scope.")
            lines.append("")

    lines.append("---")
    lines.append(f"*Generated by `{AGENT_NAME}` on {datetime.now(timezone.utc).isoformat()}*")

    path.write_text("\n".join(lines), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(domain: str | None = None,
        page_types: tuple[str, ...] = DEFAULT_PAGE_TYPES,
        workers: int = DEFAULT_WORKERS,
        skip_crawl: bool = False,
        dry_run: bool = False) -> dict:
    start = time.monotonic()
    run_date = date.today()

    pages = load_pages(domain, page_types)
    if not pages:
        msg = "No pages match the filter"
        if domain: msg += f" (domain={domain})"
        msg += f" (page_types={list(page_types)})"
        logger.warning("%s", msg)
        return {"status": "skipped", "reason": "no pages"}

    logger.info("Analyzing %d pages (workers=%d, skip_crawl=%s)",
                len(pages), workers, skip_crawl)

    # 1. Crawl (or load existing graph)
    edges_with_anchor: list[tuple[str, str, str | None]] = []
    crawl_stats = {"fetched_ok": 0, "fetch_errors": 0, "pages_with_no_links": 0}
    inserted_edges = 0

    if skip_crawl:
        existing = load_existing_edges(domain)
        edges = existing
        logger.info("Skipping crawl; loaded %d existing edges from internal_links", len(edges))
    else:
        crawler = Crawler()
        edges_with_anchor, crawl_stats = crawl_and_collect_links(pages, crawler, workers)
        if not dry_run:
            inserted_edges = upsert_edges(edges_with_anchor, dry_run)
            logger.info("Inserted %d new internal_links rows (existing rows preserved)",
                        inserted_edges)
        edges = [(src, tgt) for src, tgt, _ in edges_with_anchor]

    # 2. Compute PageRank
    pagerank = compute_pagerank(edges)
    logger.info("PageRank computed: %d nodes, %d edges", len(pagerank), len(edges))

    # 3. Findings
    scope_norm = {normalize_url(p["url"]) for p in pages}
    orphans = find_orphans(edges, scope_norm)
    dead_ends = find_dead_ends(edges, scope_norm)
    underlinked = find_underlinked(edges, pages)

    # 4. Write issues
    issue_counters = write_findings(
        pages=pages, edges=edges, orphans=orphans, dead_ends=dead_ends,
        underlinked=underlinked, dry_run=dry_run,
    )

    # 5. Write report
    report_path = None
    if not dry_run:
        report_path = write_report(
            run_date=run_date, domain=domain,
            pages=pages, edges=edges, pagerank=pagerank,
            orphans=orphans, dead_ends=dead_ends, underlinked=underlinked,
        )

    duration = time.monotonic() - start

    if not dry_run:
        record_agent_run(
            agent_name=AGENT_NAME,
            status="success" if crawl_stats["fetch_errors"] == 0 else "partial",
            records_processed=crawl_stats["fetched_ok"] if not skip_crawl else len(pages),
            errors=[],
            duration_seconds=round(duration, 2),
            metadata={
                "run_date":         run_date.isoformat(),
                "domain":           domain,
                "page_types":       list(page_types),
                "skip_crawl":       skip_crawl,
                "pages_in_scope":   len(pages),
                "graph_nodes":      len(pagerank),
                "graph_edges":      len(edges),
                "edges_inserted":   inserted_edges,
                "orphans":          len(orphans),
                "dead_ends":        len(dead_ends),
                "underlinked":      len(underlinked),
                "issues_inserted":  issue_counters["inserted"],
                "issues_updated":   issue_counters["updated"],
                "issues_resolved":  issue_counters["resolved"],
                "report_path":      str(report_path) if report_path else None,
                **crawl_stats,
            },
        )

    # 6. Console summary
    print()
    print(f"  {'=' * 72}")
    print(f"   INTERNAL LINK ANALYZER — {run_date.isoformat()}{'  [DRY RUN]' if dry_run else ''}")
    print(f"  {'=' * 72}")
    print()
    print(f"  Pages in scope:        {len(pages)}")
    print(f"  Graph nodes / edges:   {len(pagerank)} / {len(edges)}")
    if not skip_crawl:
        print(f"  Fetched OK:            {crawl_stats['fetched_ok']}")
        print(f"  Fetch errors:          {crawl_stats['fetch_errors']}")
        print(f"  Edges inserted:        {inserted_edges}")
    print(f"  Orphan pages:          {len(orphans)}  (of which priority: {sum(1 for u in orphans if next((p['page_type'] for p in pages if normalize_url(p['url']) == u), None) in ('home', 'pillar', 'service'))})")
    print(f"  Dead-end pages:        {len(dead_ends)}")
    print(f"  Under-linked pillars:  {sum(1 for u in underlinked if u['page_type'] == 'pillar')}")
    print(f"  Under-linked services: {sum(1 for u in underlinked if u['page_type'] == 'service')}")
    if not dry_run:
        print(f"  Issues inserted:       {issue_counters['inserted']}")
        print(f"  Issues updated:        {issue_counters['updated']}")
        print(f"  Issues resolved:       {issue_counters['resolved']}")
        if report_path:
            print(f"  Report:                {report_path.relative_to(Path.cwd()) if str(report_path).startswith(str(Path.cwd())) else report_path}")
    print(f"  Duration:              {duration:.1f}s")
    print()

    # Top-3 highest-PR pages in scope (quick teaser)
    in_scope_pr = sorted(
        ((u, pr) for u, pr in pagerank.items() if u in scope_norm),
        key=lambda x: -x[1],
    )[:5]
    if in_scope_pr:
        print("  Highest-PageRank pages in scope:")
        for u, pr in in_scope_pr:
            print(f"    PR={pr:.5f}  {u}")
        print()

    return {
        "status":           "success" if crawl_stats["fetch_errors"] == 0 else "partial",
        "duration_seconds": round(duration, 2),
        "graph_nodes":      len(pagerank),
        "graph_edges":      len(edges),
        "orphans":          len(orphans),
        "dead_ends":        len(dead_ends),
        "underlinked":      len(underlinked),
        "report_path":      str(report_path) if report_path else None,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Damco Internal Link Analyzer")
    parser.add_argument("--domain", help="Restrict to one domain")
    parser.add_argument("--page-types", default=",".join(DEFAULT_PAGE_TYPES),
                        help=f"Comma-separated page types (default: {','.join(DEFAULT_PAGE_TYPES)})")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS,
                        help=f"Parallel workers (default: {DEFAULT_WORKERS})")
    parser.add_argument("--skip-crawl", action="store_true",
                        help="Use existing internal_links rows; don't re-crawl")
    parser.add_argument("--dry-run", action="store_true",
                        help="Compute + analyze but don't write")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s  %(name)s  %(levelname)s  %(message)s",
    )

    page_types = tuple(p.strip() for p in args.page_types.split(",") if p.strip())

    run(domain=args.domain, page_types=page_types, workers=args.workers,
        skip_crawl=args.skip_crawl, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
