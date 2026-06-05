"""
Platform Finder — Phase 2 module of Off-Page & Links
=====================================================

Discovers outreach targets by mining competitor backlinks. The pitch
is simple: every domain that links to ≥2 of Damco's tracked competitors
but doesn't yet link to Damco is a high-confidence outreach prospect.

This module reads `competitor_backlinks` (populated by
competitive_intelligence.backlink_analyzer when the DataForSEO
Backlinks subscription is active) plus `backlinks` (Damco's own
inventory) and computes the gap.

Quality gates
-------------
- DA < 20 → drop (spam/PBN risk)
- Domain blacklist (typical scraper aggregator domains, hardcoded list)
- Damco's own domains → drop
- Already in `platform_targets` with `status='blacklist'` or `status='exhausted'` → drop
- Niche relevance score (rule-based token overlap with Damco's offerings)

Scoring
-------
For each candidate platform we compute:
  base_score          = number of distinct competitors linking from it
  da_bonus            = max(0, (avg_DA - 30) / 5)
  niche_relevance     = lexical overlap between the platform's domain
                        / tld / known niche label and Damco's offering tokens
  recency_bonus       = +1 if linked in the last 90 days
  total_score         = base * 10 + da_bonus + niche_relevance * 5 + recency_bonus * 3

Top N (default 50) get upserted into `platform_targets` with
`status='pending'` so the outreach drafter / executive can review.

Outputs
-------
- `platform_targets` rows (idempotent on platform_url)
- `outputs/audits/platforms_<offering>_<date>.md` — review report

Usage
-----
    # Top 50 prospects across all offerings (default)
    python -m offpage_links.platform_finder

    # Limit to one offering's competitors
    python -m offpage_links.platform_finder --offering "AI"

    # Tighter quality gate
    python -m offpage_links.platform_finder --min-da 40

    # Dry run (analyze + report, skip DB writes)
    python -m offpage_links.platform_finder --dry-run

Design notes
------------
- This is the ONE pre-outreach gate. If a platform makes it through
  here, the outreach drafter can be invoked without re-checking quality.
- The module gracefully no-ops when `competitor_backlinks` is empty
  (subscription not active yet) and explains the dependency.
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
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from common.database import connection, fetch_all, fetch_one, record_agent_run


logger = logging.getLogger("platform_finder")
AGENT_NAME = "offpage_links.platform_finder"

OUTPUT_DIR = Path(__file__).resolve().parent.parent / "outputs" / "audits"

DEFAULT_MIN_DA = 20
DEFAULT_TOP_N = 50
DEFAULT_MIN_COMPETITORS = 2

# Damco's own and sister-brand domains — never recommended as outreach targets
DAMCO_DOMAINS = {
    "damcogroup.com", "damcodigital.com", "achieva.ai",
}

# Aggregators / spam-prone domains that are technically real but not editorial
# outreach targets. Add aggressively when you see junk in the report.
DOMAIN_BLACKLIST = {
    "g2.com", "capterra.com", "trustpilot.com", "sitejabber.com",
    "facebook.com", "twitter.com", "x.com", "linkedin.com",
    "instagram.com", "pinterest.com", "youtube.com", "reddit.com",
    "quora.com", "medium.com", "wordpress.com", "blogspot.com",
    "github.com", "stackoverflow.com",
    # Aggregator-style "best of" listing factories — low conversion outreach
    "designrush.com", "goodfirms.co", "topdevelopers.co",
}

# Niche tokens by Damco offering → drives niche_relevance scoring
OFFERING_TOKENS: dict[str, set[str]] = {
    "AI":                              {"ai", "artificial", "intelligence", "ml", "agent", "agentic", "llm"},
    "Insurance":                       {"insurance", "insurtech", "underwriting", "claims", "policy"},
    "BPM":                             {"bpm", "process", "workflow", "automation"},
    "Business Process Automation":     {"automation", "bpa", "rpa", "workflow"},
    "Microsoft":                       {"microsoft", "azure", "dynamics", "power"},
    "AS400":                           {"as400", "iseries", "ibm", "rpg", "legacy"},
    "OutSystems":                      {"outsystems", "lowcode", "low-code"},
    "Web3":                            {"web3", "blockchain", "crypto", "defi", "nft"},
    "Achieva":                         {"salesforce", "crm", "sfdc"},
    "Cloud Consulting & Migration":    {"cloud", "azure", "aws", "gcp", "migration"},
    "Data and Visualization":          {"data", "tableau", "powerbi", "analytics", "viz"},
    "App Services & Integrations":     {"integration", "api", "esb", "middleware"},
    "Staffing":                        {"staffing", "talent", "recruitment", "team"},
    "Tech Strategy":                   {"strategy", "advisory", "transformation"},
}


# ---------------------------------------------------------------------------
# Read phase
# ---------------------------------------------------------------------------

def detect_competitor_backlinks_table() -> bool:
    """Check whether the competitor_backlinks table has any rows."""
    row = fetch_one("SELECT count(*) AS n FROM competitor_backlinks")
    return bool(row and row["n"] > 0)


def load_competitor_backlinks(offering: str | None) -> list[dict]:
    """
    Pull competitor backlinks. Joins to `competitors` to filter by offering /
    threat tier. Schema (from migration 008) is expected to have at minimum:
      competitor_id, source_url, source_domain, domain_authority, anchor,
      first_seen, last_seen, dofollow
    """
    params: list = []
    sql = """
        SELECT cb.source_url, cb.source_domain, cb.domain_authority,
               cb.anchor, cb.first_seen, cb.last_seen, cb.dofollow,
               c.competitor_domain, c.threat_tier, c.category, c.offering
          FROM competitor_backlinks cb
          JOIN competitors c ON c.id = cb.competitor_id
         WHERE cb.source_domain IS NOT NULL
    """
    if offering:
        sql += " AND c.offering = %s"
        params.append(offering)
    return fetch_all(sql, params)


def load_damco_linking_domains() -> set[str]:
    """Domains already linking to Damco — exclude from prospects."""
    rows = fetch_all("SELECT DISTINCT source_domain FROM backlinks WHERE source_domain IS NOT NULL")
    return {(r["source_domain"] or "").lower().lstrip("www.") for r in rows}


def load_platform_blocklist() -> set[str]:
    rows = fetch_all(
        "SELECT platform_url FROM platform_targets "
        "WHERE status IN ('blacklist', 'exhausted')"
    )
    return {_root_domain(r["platform_url"]) for r in rows}


def _root_domain(url: str) -> str:
    if not url:
        return ""
    if "://" not in url:
        url = "https://" + url
    host = urlparse(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def niche_relevance(domain: str, offering_tokens_set: set[str]) -> float:
    """0..3 score for how well the domain's tokens match Damco offerings."""
    if not offering_tokens_set:
        return 0.0
    tokens = set(re.findall(r"[a-z0-9]+", domain.lower()))
    matches = tokens & offering_tokens_set
    return min(3.0, len(matches) * 1.5)


def days_since(d) -> int | None:
    """Days since a date or datetime. Returns None on missing."""
    if not d:
        return None
    if hasattr(d, "date"):
        d = d.date()
    return (date.today() - d).days


def score_candidate(competitors: set[str], avg_da: float, latest_link_age_days: int | None,
                     niche_score: float) -> float:
    base = len(competitors) * 10
    da_bonus = max(0.0, (avg_da - 30) / 5.0)
    recency_bonus = 3.0 if (latest_link_age_days is not None and latest_link_age_days <= 90) else 0.0
    return round(base + da_bonus + niche_score * 5 + recency_bonus, 2)


# ---------------------------------------------------------------------------
# Process
# ---------------------------------------------------------------------------

def aggregate_candidates(rows: list[dict],
                          damco_domains_linking: set[str],
                          platform_blocklist: set[str],
                          min_competitors: int,
                          min_da: int,
                          offerings_in_scope: list[str]) -> list[dict]:
    """
    Group competitor backlinks by source_domain → candidate platform.
    Apply quality gates. Score remaining. Returns sorted candidates.
    """
    offering_tokens_set: set[str] = set()
    for o in offerings_in_scope:
        offering_tokens_set |= OFFERING_TOKENS.get(o, set())

    by_domain: dict[str, dict] = {}

    for r in rows:
        domain = (r.get("source_domain") or "").lower().lstrip("www.")
        if not domain:
            continue

        # Quality gates
        if domain in DAMCO_DOMAINS or domain in damco_domains_linking:
            continue
        if domain in DOMAIN_BLACKLIST or domain in platform_blocklist:
            continue

        bucket = by_domain.setdefault(domain, {
            "domain":              domain,
            "competitors":         set(),
            "competitor_offerings": set(),
            "da_scores":           [],
            "anchors":              [],
            "latest_seen":         None,
            "dofollow_count":       0,
            "nofollow_count":       0,
            "total_links":          0,
            "example_source_urls":  [],
        })
        bucket["competitors"].add(r["competitor_domain"])
        if r.get("offering"):
            bucket["competitor_offerings"].add(r["offering"])
        if r.get("domain_authority") is not None:
            bucket["da_scores"].append(int(r["domain_authority"]))
        if r.get("anchor"):
            bucket["anchors"].append(r["anchor"])
        bucket["total_links"] += 1
        if r.get("dofollow") is True:
            bucket["dofollow_count"] += 1
        elif r.get("dofollow") is False:
            bucket["nofollow_count"] += 1
        seen = r.get("last_seen") or r.get("first_seen")
        if seen and (bucket["latest_seen"] is None or seen > bucket["latest_seen"]):
            bucket["latest_seen"] = seen
        if r.get("source_url") and len(bucket["example_source_urls"]) < 3:
            bucket["example_source_urls"].append(r["source_url"])

    candidates: list[dict] = []
    for d, b in by_domain.items():
        if len(b["competitors"]) < min_competitors:
            continue
        avg_da = (sum(b["da_scores"]) / len(b["da_scores"])) if b["da_scores"] else 0.0
        if avg_da < min_da:
            continue

        niche = niche_relevance(d, offering_tokens_set)
        latest_age = days_since(b["latest_seen"])
        total_score = score_candidate(
            competitors=b["competitors"],
            avg_da=avg_da,
            latest_link_age_days=latest_age,
            niche_score=niche,
        )

        candidates.append({
            "platform_url":         d,
            "platform_name":        d,
            "domain_authority":     int(round(avg_da)) if b["da_scores"] else None,
            "competitors":          sorted(b["competitors"]),
            "competitor_offerings": sorted(b["competitor_offerings"]),
            "competitor_count":     len(b["competitors"]),
            "total_links":          b["total_links"],
            "dofollow":             b["dofollow_count"],
            "nofollow":             b["nofollow_count"],
            "niche_relevance":      niche,
            "days_since_last_link": latest_age,
            "example_source_urls":  b["example_source_urls"],
            "score":                total_score,
        })

    candidates.sort(key=lambda c: -c["score"])
    return candidates


# ---------------------------------------------------------------------------
# Write phase
# ---------------------------------------------------------------------------

def upsert_platforms(candidates: list[dict], top_n: int) -> int:
    """Upsert top N into platform_targets. Returns count inserted (vs updated)."""
    inserted = 0
    sql = """
        INSERT INTO platform_targets
            (platform_url, platform_name, domain_authority, niche,
             contact_info, response_rate, quality_score, status)
        VALUES (%s, %s, %s, %s, %s::jsonb, NULL, %s, 'pending')
        ON CONFLICT (platform_url) DO UPDATE SET
            domain_authority = COALESCE(EXCLUDED.domain_authority, platform_targets.domain_authority),
            niche            = COALESCE(EXCLUDED.niche, platform_targets.niche),
            quality_score    = GREATEST(COALESCE(platform_targets.quality_score, 0), EXCLUDED.quality_score)
        RETURNING (xmax = 0) AS inserted
    """
    with connection() as conn:
        with conn.cursor() as cur:
            for c in candidates[:top_n]:
                niche = ", ".join(c["competitor_offerings"]) or None
                contact_info = {
                    "discovered_via":      "competitor_backlinks",
                    "linking_competitors": c["competitors"],
                    "example_source_urls": c["example_source_urls"],
                }
                cur.execute(
                    sql,
                    (
                        c["platform_url"],
                        c["platform_name"],
                        c["domain_authority"],
                        niche,
                        json.dumps(contact_info),
                        c["score"],
                    ),
                )
                if cur.fetchone()[0]:
                    inserted += 1
    return inserted


def write_markdown(candidates: list[dict], offering: str | None,
                   inserted: int, top_n: int, db_blocked: bool) -> Path:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    suffix = f"_{offering.replace(' ', '_').replace('/', '-')}" if offering else ""
    path = OUTPUT_DIR / f"platforms_{date.today().isoformat()}{suffix}.md"

    p: list[str] = []
    p.append(f"# Outreach Platform Prospects — {date.today().isoformat()}")
    p.append("")
    p.append(f"_Generated by `{AGENT_NAME}`._")
    if offering:
        p.append(f"_Scope: `{offering}`_")
    p.append("")

    if db_blocked:
        p.append("## ⚠️ Blocked")
        p.append("")
        p.append("- `competitor_backlinks` table is empty. Run `competitive_intelligence.backlink_analyzer` first.")
        p.append("- That module requires an active **DataForSEO Backlinks subscription** (~$99/mo).")
        path.write_text("\n".join(p), encoding="utf-8")
        return path

    p.append("## Summary")
    p.append("")
    p.append("| Metric | Value |")
    p.append("|---|---:|")
    p.append(f"| Total candidates surfaced | {len(candidates)} |")
    p.append(f"| Top N persisted to `platform_targets` | {min(len(candidates), top_n)} |")
    p.append(f"| New (vs updates) | {inserted} |")
    p.append("")

    if not candidates:
        p.append("_No candidate platforms passed quality gates. Lower `--min-da` or `--min-competitors`._")
        path.write_text("\n".join(p), encoding="utf-8")
        return path

    p.append(f"## Top {min(len(candidates), top_n)} prospects")
    p.append("")
    p.append("| # | Platform | Score | Competitors | Avg DA | Niche match | Days since last link | Offerings |")
    p.append("|---:|---|---:|---:|---:|---:|---:|---|")
    for i, c in enumerate(candidates[:top_n], 1):
        offerings = ", ".join(c["competitor_offerings"])[:60]
        days = c["days_since_last_link"]
        days_str = f"{days}d" if days is not None else "—"
        p.append(f"| {i} | `{c['platform_url']}` | {c['score']} | {c['competitor_count']} | "
                 f"{c['domain_authority'] or '—'} | {c['niche_relevance']:.1f} | "
                 f"{days_str} | {offerings} |")
    p.append("")

    # Top 10 detail
    p.append("## Detail — top 10")
    p.append("")
    for i, c in enumerate(candidates[:10], 1):
        p.append(f"### {i}. `{c['platform_url']}`  (score {c['score']})")
        p.append("")
        p.append(f"- Competitors linking from this site: {', '.join(c['competitors'])}")
        p.append(f"- Avg DA: {c['domain_authority']} | Dofollow: {c['dofollow']} / Nofollow: {c['nofollow']}")
        p.append(f"- Niche relevance: {c['niche_relevance']:.1f}")
        if c["competitor_offerings"]:
            p.append(f"- Offerings this platform writes about: {', '.join(c['competitor_offerings'])}")
        if c["example_source_urls"]:
            p.append("- Example linking pages:")
            for u in c["example_source_urls"]:
                p.append(f"  - `{u}`")
        p.append("")

    path.write_text("\n".join(p), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(offering: str | None = None,
        min_da: int = DEFAULT_MIN_DA,
        min_competitors: int = DEFAULT_MIN_COMPETITORS,
        top_n: int = DEFAULT_TOP_N,
        dry_run: bool = False) -> dict:
    start = time.monotonic()

    if not detect_competitor_backlinks_table():
        md_path = write_markdown([], offering, 0, top_n, db_blocked=True)
        logger.warning("competitor_backlinks is empty — platform_finder needs that table populated first.")
        if not dry_run:
            record_agent_run(
                agent_name=AGENT_NAME,
                status="partial",
                records_processed=0,
                errors=["competitor_backlinks empty — DataForSEO Backlinks subscription required"],
                duration_seconds=round(time.monotonic() - start, 2),
                metadata={"db_blocked": True, "md_path": str(md_path)},
            )
        return {"status": "partial", "reason": "competitor_backlinks empty",
                "md_path": str(md_path)}

    rows = load_competitor_backlinks(offering)
    damco_linkers = load_damco_linking_domains()
    blocklist = load_platform_blocklist()
    offerings_scope = [offering] if offering else list(OFFERING_TOKENS.keys())

    logger.info("Loaded %d competitor-backlink rows; %d damco-linkers; %d blacklisted",
                len(rows), len(damco_linkers), len(blocklist))

    candidates = aggregate_candidates(
        rows, damco_linkers, blocklist,
        min_competitors=min_competitors, min_da=min_da,
        offerings_in_scope=offerings_scope,
    )
    logger.info("Surfaced %d candidate platform(s) after quality gates", len(candidates))

    inserted = 0
    if not dry_run:
        inserted = upsert_platforms(candidates, top_n)

    md_path = write_markdown(candidates, offering, inserted, top_n, db_blocked=False)

    duration = time.monotonic() - start

    if not dry_run:
        record_agent_run(
            agent_name=AGENT_NAME,
            status="success",
            records_processed=min(len(candidates), top_n),
            errors=[],
            duration_seconds=round(duration, 2),
            metadata={
                "offering":        offering,
                "min_da":          min_da,
                "min_competitors": min_competitors,
                "candidates_total": len(candidates),
                "inserted":         inserted,
                "top_n":            top_n,
                "md_path":          str(md_path),
            },
        )

    print()
    print(f"  {'=' * 72}")
    print(f"   PLATFORM FINDER -- {date.today().isoformat()}")
    print(f"  {'=' * 72}")
    print()
    print(f"  Scope offering:        {offering or 'all'}")
    print(f"  Min DA:                {min_da}")
    print(f"  Candidates surfaced:   {len(candidates)}")
    print(f"  Top N persisted:       {min(len(candidates), top_n)}")
    print(f"  New platform rows:     {inserted}")
    print(f"  Report:                {md_path}")
    print(f"  Duration:              {duration:.2f}s")
    if candidates[:5]:
        print()
        print("  Top 5 prospects:")
        for c in candidates[:5]:
            print(f"    {c['score']:>6.1f}  {c['platform_url']:<40} "
                  f"({c['competitor_count']} competitors, DA={c['domain_authority'] or '?'})")
    print()

    return {
        "status":           "success",
        "candidates":       len(candidates),
        "persisted":        min(len(candidates), top_n),
        "inserted":         inserted,
        "md_path":          str(md_path),
        "duration_seconds": round(duration, 2),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Damco Outreach Platform Finder")
    parser.add_argument("--offering", help="Restrict to one Damco offering")
    parser.add_argument("--min-da", type=int, default=DEFAULT_MIN_DA,
                        help=f"Minimum average DA per candidate (default: {DEFAULT_MIN_DA})")
    parser.add_argument("--min-competitors", type=int, default=DEFAULT_MIN_COMPETITORS,
                        help=f"Minimum distinct competitors linking from a candidate "
                             f"(default: {DEFAULT_MIN_COMPETITORS})")
    parser.add_argument("--top-n", type=int, default=DEFAULT_TOP_N,
                        help=f"How many candidates to persist (default: {DEFAULT_TOP_N})")
    parser.add_argument("--dry-run", action="store_true",
                        help="Skip DB writes")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s  %(name)s  %(levelname)s  %(message)s",
    )

    run(offering=args.offering,
        min_da=args.min_da,
        min_competitors=args.min_competitors,
        top_n=args.top_n,
        dry_run=args.dry_run)


if __name__ == "__main__":
    main()
