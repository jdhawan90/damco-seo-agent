"""
Agent registry — every activity in the system, declared.
=========================================================

Each module under an agent folder is an agent: it has a trigger, a contract,
a persistent record in `agent_runs`, and a failure mode. Some are AI-assisted
and some are pure deterministic automation. Both are agents; the difference
is whether there is a genuine language judgment to make.

Before this module the identity existed only as a string passed to
`record_agent_run()` at call time, and the human-readable inventory lived in
markdown tables that drifted — at one point the root CLAUDE.md listed four
shipped agents as "Planned".

Why a central catalogue rather than a manifest per module
---------------------------------------------------------
Colocating a spec next to its code is the more obvious design, and it was the
first plan. It was rejected because a spec sitting beside the code it
describes still rots silently — nothing forces the two to agree.

What actually prevents rot is `validate()`, which walks the agent folders,
extracts every `AGENT_NAME`, and fails if the catalogue and the filesystem
disagree in either direction. Given that check, a single sorted list is
easier to read, easier to diff, and does not require importing 21 modules to
answer "what agents exist".

Usage
-----
    python -m common.agents                # human-readable inventory
    python -m common.agents --json         # machine-readable
    python -m common.agents --validate     # CI check: catalogue vs filesystem
    python -m common.agents --sync         # write the catalogue into `agents`
"""

from __future__ import annotations

import argparse
import ast
import json
import logging
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Literal


logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent

AGENT_FOLDERS = (
    "keyword_intelligence",
    "technical_seo",
    "offpage_links",
    "content_operations",
    "competitive_intelligence",
    "content_assets",
    "sales_enablement",
)

Kind = Literal["deterministic", "ai_assisted"]
Tier = Literal["cheap", "default", "complex"]


@dataclass(frozen=True)
class AgentSpec:
    """
    One agent.

    `kind` is the honest label, not an aspiration. "deterministic" is not a
    lesser status — seven of these should never call a model, and saying so
    explicitly is what stops someone adding one later "for consistency".
    """
    name: str                       # matches AGENT_NAME and agent_runs.agent_name
    title: str
    kind: Kind
    summary: str
    reads: tuple[str, ...] = ()
    writes: tuple[str, ...] = ()
    llm_tier: Tier | None = None    # None for deterministic agents
    cadence_days: int | None = None # None = on demand
    blocked_by: str | None = None   # external dependency, if any
    notes: str = ""

    @property
    def folder(self) -> str:
        return self.name.split(".", 1)[0]

    @property
    def module(self) -> str:
        return self.name.split(".", 1)[1]

    def __post_init__(self) -> None:
        if self.kind == "ai_assisted" and self.llm_tier is None:
            raise ValueError(f"{self.name}: ai_assisted agents must declare an llm_tier")
        if self.kind == "deterministic" and self.llm_tier is not None:
            raise ValueError(
                f"{self.name}: deterministic agents must not declare an llm_tier — "
                f"if it calls a model, it is ai_assisted"
            )


# ---------------------------------------------------------------------------
# The catalogue
# ---------------------------------------------------------------------------

CATALOGUE: tuple[AgentSpec, ...] = (

    # -- keyword_intelligence ------------------------------------------------
    AgentSpec(
        name="keyword_intelligence.rank_tracker",
        title="Rank Tracker",
        kind="ai_assisted",
        llm_tier="cheap",
        summary="Pulls SERP positions for due keywords, records our position plus "
                "the full top 10, and emits competitor SERP events.",
        reads=("keywords", "keyword_serp_snapshots"),
        writes=("keyword_rankings", "keyword_serp_snapshots", "competitor_rankings",
                "competitors", "competitor_serp_events"),
        cadence_days=14,
        notes="Costs ~$0.00465 per keyword per run on the standard queue. "
              "The ranking pipeline itself is arithmetic and must stay so — "
              "executives reconcile these numbers week to week. The single LLM "
              "touchpoint is categorizing newly-stubbed competitors where "
              "category IS NULL, after the ranking data is already written.",
    ),
    AgentSpec(
        name="keyword_intelligence.gsc_enrichment",
        title="GSC Enrichment",
        kind="deterministic",
        summary="Matches Search Console query metrics onto tracked keywords.",
        reads=("keywords",),
        writes=("keyword_rankings",),
        cadence_days=14,
        notes="Deliberately never calls a model. Query matching wants normalized "
              "or embedding similarity, not an LLM — a non-deterministic match "
              "would jitter position history for reasons unrelated to ranking.",
    ),
    AgentSpec(
        name="keyword_intelligence.reports",
        title="Ranking Reports",
        kind="ai_assisted",
        llm_tier="default",
        summary="Renders the executive Excel workbook from stored rankings, "
                "including a narrated Executive Summary sheet.",
        reads=("keyword_rankings", "keywords"),
        writes=(),
        notes="No agent_runs row today — it is a pure renderer invoked by hand. "
              "The narrative receives pre-computed aggregates only; every figure "
              "in the workbook is calculated in Python. --no-narrative skips it.",
    ),
    AgentSpec(
        name="keyword_intelligence.trend_scout",
        title="Trend Scout",
        kind="ai_assisted",
        llm_tier="cheap",
        summary="Harvests industry feeds, extracts recurring phrases, prices them "
                "against Keyword Planner, and proposes ones we do not track.",
        reads=("trend_sources", "trend_mentions", "keywords"),
        writes=("trend_mentions", "keyword_candidates"),
        cadence_days=7,
        notes="Promotion into `keywords` is a human gate: discovery is ~$0.05/run, "
              "tracking is a recurring per-keyword cost forever.",
    ),

    # -- competitive_intelligence -------------------------------------------
    AgentSpec(
        name="competitive_intelligence.competitor_monitor",
        title="Competitor Monitor",
        kind="deterministic",
        summary="Crawls competitor pages and diffs them against stored state.",
        reads=("competitors", "competitor_rankings", "competitor_pages"),
        writes=("competitor_pages", "competitor_changes"),
        cadence_days=7,
    ),
    AgentSpec(
        name="competitive_intelligence.content_monitor",
        title="Competitor Content Monitor",
        kind="deterministic",
        summary="Walks competitor sitemaps and fires events for newly published URLs.",
        reads=("competitors", "competitor_published_urls", "keywords"),
        writes=("competitor_published_urls", "competitor_changes"),
        cadence_days=7,
    ),
    AgentSpec(
        name="competitive_intelligence.gap_analyzer",
        title="Gap Analyzer",
        kind="ai_assisted",
        llm_tier="default",
        summary="Classifies every keyword as coverage gap, displacement or "
                "low priority against tracked competitors.",
        reads=("keywords", "keyword_rankings", "competitor_rankings", "competitors"),
        writes=(),
        notes="Narrative only when --with-narrative. The classification itself is "
              "threshold arithmetic and stays deterministic.",
    ),
    AgentSpec(
        name="competitive_intelligence.event_digest",
        title="SERP Event Digest",
        kind="ai_assisted",
        llm_tier="default",
        summary="Renders a severity-filtered digest of competitor SERP events "
                "since the last run.",
        reads=("competitor_serp_events", "agent_runs"),
        writes=(),
        cadence_days=7,
    ),
    AgentSpec(
        name="competitive_intelligence.backlink_analyzer",
        title="Competitor Backlink Analyzer",
        kind="deterministic",
        summary="Pulls competitor backlink profiles and finds domains linking to "
                "multiple competitors.",
        reads=("competitors",),
        writes=("competitor_backlinks",),
        cadence_days=30,
        blocked_by="DataForSEO Backlinks subscription",
    ),

    # -- technical_seo -------------------------------------------------------
    AgentSpec(
        name="technical_seo.sitemap_validator",
        title="Sitemap Validator",
        kind="ai_assisted",
        llm_tier="cheap",
        summary="Walks each owned sitemap, validates every URL, and classifies "
                "page types into the `pages` table.",
        reads=("pages",),
        writes=("pages", "technical_issues"),
        cadence_days=14,
        notes="URL validation and the path rules are deterministic. The model "
              "only sees URLs the rules returned None for, one batched call, "
              "cached in pages.page_type. --no-llm skips it.",
    ),
    AgentSpec(
        name="technical_seo.site_auditor",
        title="Site Auditor",
        kind="ai_assisted",
        llm_tier="cheap",
        summary="Crawls in-scope pages and runs 15 on-page detectors plus a "
                "cross-page duplicate pass.",
        reads=("pages",),
        writes=("pages", "technical_issues"),
        cadence_days=14,
        notes="Opens, updates and auto-resolves technical_issues. Correctness "
              "depends on the same input producing the same issue set every run, "
              "so no model sits in the detector path. The one LLM call names the "
              "likely shared cause of clustered issues, once per run, to the "
              "console only — it never touches technical_issues.",
    ),
    AgentSpec(
        name="technical_seo.cwv_monitor",
        title="Core Web Vitals Monitor",
        kind="deterministic",
        summary="Samples PageSpeed Insights per URL and device, and opens issues "
                "on threshold breaches and regressions.",
        reads=("pages", "cwv_metrics"),
        writes=("cwv_metrics", "technical_issues"),
        cadence_days=7,
        notes="Threshold comparison and percentage deltas on numbers from an API. "
              "There is nothing here for a model to judge.",
    ),
    AgentSpec(
        name="technical_seo.internal_link_analyzer",
        title="Internal Link Analyzer",
        kind="deterministic",
        summary="Builds the internal link graph, computes PageRank, and finds "
                "orphans, dead ends and under-linked priority pages.",
        reads=("pages", "internal_links"),
        writes=("internal_links", "technical_issues"),
        notes="Anchor text is collected and stored but never read back — the "
              "highest-value AI addition in this folder is a batch pass over it.",
    ),

    # -- content_operations --------------------------------------------------
    AgentSpec(
        name="content_operations.brief_generator",
        title="Content Brief Generator",
        kind="ai_assisted",
        llm_tier="default",
        summary="Turns a coverage-gap keyword into a writable SEO brief with "
                "outline, angle and internal-link targets.",
        reads=("keywords", "keyword_rankings", "competitor_rankings", "pages"),
        writes=("content_briefs",),
    ),
    AgentSpec(
        name="content_operations.compliance_checker",
        title="Content Compliance Checker",
        kind="deterministic",
        summary="Scores a submitted draft against its brief across 12 weighted "
                "mechanical SEO dimensions.",
        reads=("content_briefs", "pages"),
        writes=("compliance_checks",),
        notes="No brand-voice or banned-word dimension exists yet. Adding one "
              "means re-weighting all twelve — the weights assert to 100.",
    ),
    AgentSpec(
        name="content_operations.glossary_detector",
        title="Glossary Gap Detector",
        kind="deterministic",
        summary="Finds definition-intent keywords with no glossary page.",
        reads=("keywords", "pages", "keyword_rankings"),
        writes=(),
    ),
    AgentSpec(
        name="content_operations.concentration_checker",
        title="Calendar Concentration Checker",
        kind="deterministic",
        summary="Flags over-concentration in the brief pipeline across offering, "
                "stage, page type and intent.",
        reads=("content_briefs", "keywords"),
        writes=(),
        notes="Counting briefs into buckets. Never a model.",
    ),

    # -- offpage_links -------------------------------------------------------
    AgentSpec(
        name="offpage_links.backlink_tracker",
        title="Backlink Tracker",
        kind="deterministic",
        summary="Refreshes our own backlink inventory from DataForSEO, with GSC "
                "as weak cross-source confirmation.",
        reads=("pages", "backlinks"),
        writes=("backlinks",),
        cadence_days=30,
        blocked_by="DataForSEO Backlinks subscription",
    ),
    AgentSpec(
        name="offpage_links.platform_finder",
        title="Platform Finder",
        kind="deterministic",
        summary="Mines competitor backlinks for domains linking to multiple "
                "competitors but not to us, and scores them as prospects.",
        reads=("competitor_backlinks", "backlinks", "platform_targets"),
        writes=("platform_targets",),
        blocked_by="DataForSEO Backlinks subscription",
    ),
    AgentSpec(
        name="offpage_links.outreach_drafter",
        title="Outreach Drafter",
        kind="ai_assisted",
        llm_tier="default",
        summary="Drafts a personalized pitch and follow-up for one platform target.",
        reads=("platform_targets", "pages"),
        writes=("offpage_activities",),
        notes="Never sends. Drafts are human-gated by design.",
    ),
    AgentSpec(
        name="offpage_links.guest_post_drafter",
        title="Guest Post Drafter",
        kind="ai_assisted",
        llm_tier="default",
        summary="Drafts a full third-party article with embedded CTA links, then "
                "runs a 10-check compliance scan over it.",
        reads=("platform_targets", "content_briefs"),
        writes=("offpage_activities",),
        notes="Never publishes. All 10 compliance checks are rule-based and must "
              "stay so — the model writes prose, the rules verify it.",
    ),
    AgentSpec(
        name="offpage_links.vendor_scorer",
        title="Vendor Scorer",
        kind="deterministic",
        summary="Rolls outreach activity history into platform response and "
                "quality scores, and retires chronic non-responders.",
        reads=("offpage_activities", "platform_targets", "backlinks"),
        writes=("platform_targets",),
        cadence_days=30,
    ),
)


# ---------------------------------------------------------------------------
# Lookups
# ---------------------------------------------------------------------------

BY_NAME: dict[str, AgentSpec] = {s.name: s for s in CATALOGUE}


def get(name: str) -> AgentSpec | None:
    return BY_NAME.get(name)


def by_folder(folder: str) -> tuple[AgentSpec, ...]:
    return tuple(s for s in CATALOGUE if s.folder == folder)


# ---------------------------------------------------------------------------
# Validation — the part that stops the catalogue rotting
# ---------------------------------------------------------------------------

def discover_agent_names() -> dict[str, str]:
    """
    Walk the agent folders and pull every module-level `AGENT_NAME`.

    Parsed with `ast`, not imported: importing 21 modules to answer an
    inventory question would need a database and every optional dependency.
    """
    found: dict[str, str] = {}
    for folder in AGENT_FOLDERS:
        d = REPO_ROOT / folder
        if not d.is_dir():
            continue
        for path in sorted(d.glob("*.py")):
            if path.name == "__init__.py":
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except SyntaxError:
                continue
            for node in tree.body:
                if not isinstance(node, ast.Assign):
                    continue
                for target in node.targets:
                    if (isinstance(target, ast.Name) and target.id == "AGENT_NAME"
                            and isinstance(node.value, ast.Constant)
                            and isinstance(node.value.value, str)):
                        found[node.value.value] = path.relative_to(REPO_ROOT).as_posix()
    return found


def discover_llm_users() -> set[str]:
    """
    Which agent modules actually reference `common.llm`.

    Grepped from source rather than imported, for the same reason as
    `discover_agent_names`: answering an inventory question should not
    require a database and every optional dependency.
    """
    users: set[str] = set()
    for folder in AGENT_FOLDERS:
        d = REPO_ROOT / folder
        if not d.is_dir():
            continue
        for path in sorted(d.glob("*.py")):
            if path.name == "__init__.py":
                continue
            src = path.read_text(encoding="utf-8")
            if "common.llm" in src or "from common import llm" in src:
                users.add(f"{folder}.{path.stem}")
    return users


def validate() -> list[str]:
    """Return a list of problems. Empty means catalogue and filesystem agree."""
    problems: list[str] = []
    discovered = discover_agent_names()

    for name, rel in sorted(discovered.items()):
        if name not in BY_NAME:
            problems.append(f"{rel} declares AGENT_NAME={name!r} but it is not in CATALOGUE")

    for spec in CATALOGUE:
        expected = REPO_ROOT / spec.folder / f"{spec.module}.py"
        if not expected.exists():
            problems.append(f"CATALOGUE has {spec.name!r} but {expected.relative_to(REPO_ROOT)} does not exist")
        elif spec.name not in discovered and spec.name != "keyword_intelligence.reports":
            # reports.py is a renderer with no agent_runs row; catalogued for
            # completeness of the inventory, exempt from the AGENT_NAME rule.
            problems.append(f"CATALOGUE has {spec.name!r} but that module declares no AGENT_NAME")

    # The `kind` label must match what the code actually does. This check
    # exists because the catalogue drifted within hours of being written:
    # rank_tracker and reports gained LLM calls and stayed labelled
    # "deterministic", so `--list` under-reported AI usage. A registry that
    # can quietly misdescribe the system is worse than no registry.
    llm_users = discover_llm_users()
    for spec in CATALOGUE:
        uses_llm = spec.name in llm_users
        if uses_llm and spec.kind != "ai_assisted":
            problems.append(
                f"{spec.name!r} references common.llm but is catalogued as "
                f"{spec.kind!r} — set kind='ai_assisted' and declare an llm_tier"
            )
        elif not uses_llm and spec.kind == "ai_assisted":
            problems.append(
                f"{spec.name!r} is catalogued as 'ai_assisted' but never "
                f"references common.llm — set kind='deterministic'"
            )

    return problems


# ---------------------------------------------------------------------------
# Persistence + status
# ---------------------------------------------------------------------------

def sync_registry() -> int:
    """Upsert the catalogue into the `agents` table. Returns rows written."""
    from common.database import connection

    rows = 0
    with connection() as conn:
        with conn.cursor() as cur:
            for s in CATALOGUE:
                cur.execute(
                    """
                    INSERT INTO agents
                        (name, title, folder, module, kind, summary, reads, writes,
                         llm_tier, cadence_days, blocked_by, notes)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (name) DO UPDATE SET
                        title = EXCLUDED.title, folder = EXCLUDED.folder,
                        module = EXCLUDED.module, kind = EXCLUDED.kind,
                        summary = EXCLUDED.summary, reads = EXCLUDED.reads,
                        writes = EXCLUDED.writes, llm_tier = EXCLUDED.llm_tier,
                        cadence_days = EXCLUDED.cadence_days,
                        blocked_by = EXCLUDED.blocked_by, notes = EXCLUDED.notes,
                        updated_at = now()
                    """,
                    (s.name, s.title, s.folder, s.module, s.kind, s.summary,
                     list(s.reads), list(s.writes), s.llm_tier, s.cadence_days,
                     s.blocked_by, s.notes or None),
                )
                rows += 1
        conn.commit()
    return rows


def status() -> list[dict]:
    """
    Catalogue joined to reality — last run date and status from `agent_runs`.

    This is what the agent-directory table in CLAUDE.md should have been:
    derived, not retyped.
    """
    from common.database import fetch_all

    runs = {
        r["agent_name"]: r
        for r in fetch_all(
            """
            SELECT DISTINCT ON (agent_name)
                   agent_name, run_date, status, records_processed
              FROM agent_runs
             ORDER BY agent_name, run_date DESC
            """
        )
    }
    out = []
    for s in CATALOGUE:
        r = runs.get(s.name)
        out.append({
            # folder/module are properties, so asdict() does not include them.
            **asdict(s),
            "folder":       s.folder,
            "module":       s.module,
            "last_run":     r["run_date"].isoformat() if r else None,
            "last_status":  r["status"] if r else None,
            "last_records": r["records_processed"] if r else None,
            "has_ever_run": r is not None,
        })
    return out


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Agent registry")
    parser.add_argument("--json", action="store_true", help="Machine-readable output")
    parser.add_argument("--validate", action="store_true",
                        help="Check the catalogue against the filesystem")
    parser.add_argument("--sync", action="store_true",
                        help="Write the catalogue into the `agents` table")
    parser.add_argument("--folder", help="Restrict to one agent folder")
    args = parser.parse_args()

    if args.validate:
        problems = validate()
        for p in problems:
            print(f"  PROBLEM  {p}")
        print(f"\n{len(CATALOGUE)} catalogued, {len(discover_agent_names())} discovered, "
              f"{len(problems)} problem(s)")
        sys.exit(1 if problems else 0)

    if args.sync:
        n = sync_registry()
        print(f"Synced {n} agent(s) into the registry.")
        return

    rows = status()
    if args.folder:
        rows = [r for r in rows if r["folder"] == args.folder]

    if args.json:
        print(json.dumps(rows, indent=2, default=str))
        return

    ai = sum(1 for r in rows if r["kind"] == "ai_assisted")
    never = sum(1 for r in rows if not r["has_ever_run"])
    blocked = sum(1 for r in rows if r["blocked_by"])

    print()
    print(f"  {len(rows)} agents — {ai} AI-assisted, {len(rows) - ai} deterministic, "
          f"{never} never run, {blocked} blocked")
    print(f"  {'=' * 96}")
    current = None
    for r in rows:
        if r["folder"] != current:
            current = r["folder"]
            print(f"\n  {current}")
        kind = "AI " if r["kind"] == "ai_assisted" else "   "
        last = (r["last_run"] or "never")[:10]      # date is enough here
        flag = f"  [BLOCKED: {r['blocked_by']}]" if r["blocked_by"] else ""
        print(f"    {kind} {r['module']:<26} {last:<12} {r['last_status'] or '':<9}{flag}")
    print()


if __name__ == "__main__":
    main()
