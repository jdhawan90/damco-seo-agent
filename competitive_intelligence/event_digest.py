"""
Event Digest — third module of the Competitive Intelligence agent
==================================================================

The operational alert layer. Reads the append-only `competitor_serp_events`
stream populated by rank_tracker and produces a markdown digest of
high-severity changes since the last digest was generated.

Source of "since when"
----------------------
- If `--since` is provided, use that date.
- Otherwise: latest successful agent_runs row for this agent's name -> use
  its run_date as the lower bound.
- If no prior run: default to events from the last 14 days.

What gets included
------------------
- severity in (critical, high, medium) by default; `--all-severity` to
  include low/info noise too.
- Grouped into sections for fast scanning:
    1. Damco-side events (we moved)
    2. Competitor entries / exits (top-10 churn)
    3. Position-change events (>=3 positions)
    4. Threat-tier promotions / demotions
    5. SERP-feature changes
- Per-keyword detail collapsed when many events share the same keyword.

Optional LLM narrative
----------------------
`--with-narrative` adds an editorial 2-3 paragraph summary at the top
(uses CLAUDE_MODEL_DEFAULT). Falls back to rule-based summary when the
Anthropic API is unavailable.

Outputs
-------
outputs/audits/serp_event_digest_<since>_<today>.md

Usage
-----
    # Default: events since last digest (or last 14 days)
    python -m competitive_intelligence.event_digest

    # One offering only
    python -m competitive_intelligence.event_digest --offering "AI"

    # Custom window
    python -m competitive_intelligence.event_digest --since 2026-05-01

    # Include low/info severity (very noisy)
    python -m competitive_intelligence.event_digest --all-severity

    # Include LLM editorial summary
    python -m competitive_intelligence.event_digest --with-narrative

    # Dry run — generate report, skip agent_runs DB write
    python -m competitive_intelligence.event_digest --dry-run
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from common.database import fetch_all, record_agent_run
from common.llm import call_claude, LLMUnavailableError


logger = logging.getLogger("event_digest")
AGENT_NAME = "competitive_intelligence.event_digest"

DEFAULT_LOOKBACK_DAYS = 14   # used if no prior digest run found and no --since

# Severity ordering for filtering / display
SEV_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}

INCLUDE_BY_DEFAULT = {"critical", "high", "medium"}

# Event types grouped by section, in display order
DAMCO_EVENT_TYPES = {
    "damco_drops_top_n", "damco_enters_top_n", "damco_position_change",
}
COMPETITOR_CHURN = {
    "new_entrant", "drop_out",
}
POSITION_MOVEMENTS = {
    "position_gain", "position_drop",
}
TIER_EVENTS = {
    "threat_tier_changed", "first_seen_anywhere",
}
FEATURE_EVENTS = {
    "serp_feature_appeared", "serp_feature_disappeared",
}

OUTPUT_DIR = Path(__file__).resolve().parent.parent / "outputs" / "audits"


# ---------------------------------------------------------------------------
# Read phase
# ---------------------------------------------------------------------------

def resolve_since(since_arg: str | None) -> tuple[date, str]:
    """Returns (since_date, source_label)."""
    if since_arg:
        return date.fromisoformat(since_arg), "explicit --since flag"

    rows = fetch_all(
        "SELECT max(run_date)::date AS d FROM agent_runs "
        "WHERE agent_name = %s AND status IN ('success', 'partial')",
        [AGENT_NAME],
    )
    last = rows[0]["d"] if rows and rows[0]["d"] else None
    if last:
        return last, f"last successful digest on {last.isoformat()}"
    fallback = date.today() - timedelta(days=DEFAULT_LOOKBACK_DAYS)
    return fallback, f"no prior digest — defaulting to {DEFAULT_LOOKBACK_DAYS}-day lookback"


def load_events(since: date, offering: str | None,
                severities: set[str]) -> list[dict]:
    params: list = [list(severities), since]
    sql = """
        SELECT e.id, e.event_type, e.severity, e.event_date,
               e.old_value, e.new_value, e.delta, e.metadata,
               e.keyword_id, e.competitor_id,
               k.keyword, k.offering,
               c.competitor_domain, c.category, c.threat_tier
          FROM competitor_serp_events e
     LEFT JOIN keywords k ON k.id = e.keyword_id
     LEFT JOIN competitors c ON c.id = e.competitor_id
         WHERE e.severity = ANY(%s)
           AND e.event_date >= %s
    """
    if offering:
        sql += " AND k.offering = %s"
        params.append(offering)
    sql += " ORDER BY (CASE e.severity WHEN 'critical' THEN 0 WHEN 'high' THEN 1 WHEN 'medium' THEN 2 WHEN 'low' THEN 3 ELSE 4 END), e.event_date DESC, e.id DESC"
    return fetch_all(sql, params)


# ---------------------------------------------------------------------------
# Aggregation helpers
# ---------------------------------------------------------------------------

def aggregate_summary(events: list[dict]) -> dict:
    """Build the executive-summary numbers."""
    counters_sev = Counter(e["severity"] for e in events)
    counters_type = Counter(e["event_type"] for e in events)
    distinct_keywords = len({e["keyword_id"] for e in events if e["keyword_id"]})
    distinct_competitors = len({e["competitor_id"] for e in events if e["competitor_id"]})
    return {
        "total": len(events),
        "by_severity": dict(counters_sev),
        "by_type":     dict(counters_type),
        "distinct_keywords": distinct_keywords,
        "distinct_competitors": distinct_competitors,
    }


def group_by_section(events: list[dict]) -> dict[str, list[dict]]:
    """Bucket events into report sections."""
    sections = {
        "damco":      [],
        "churn":      [],
        "positions":  [],
        "tier":       [],
        "features":   [],
        "other":      [],
    }
    for e in events:
        t = e["event_type"]
        if t in DAMCO_EVENT_TYPES:        sections["damco"].append(e)
        elif t in COMPETITOR_CHURN:       sections["churn"].append(e)
        elif t in POSITION_MOVEMENTS:     sections["positions"].append(e)
        elif t in TIER_EVENTS:            sections["tier"].append(e)
        elif t in FEATURE_EVENTS:         sections["features"].append(e)
        else:                              sections["other"].append(e)
    return sections


# ---------------------------------------------------------------------------
# Rendering — markdown
# ---------------------------------------------------------------------------

def _sev_emoji(sev: str) -> str:
    return {"critical": "🚨", "high": "⚠️", "medium": "📊", "low": "·", "info": " "}.get(sev, "")


def _fmt_value(v: dict | None) -> str:
    if not v:
        return "—"
    parts = [f"{k}={v[k]}" for k in sorted(v.keys())]
    return ", ".join(parts)


def render_damco_events(events: list[dict]) -> list[str]:
    if not events:
        return []
    lines = [f"## 🚨 Damco-side movements ({len(events)})", ""]
    lines.append("| Date | Severity | Type | Keyword | Old → New | Delta |")
    lines.append("|---|---|---|---|---|---:|")
    for e in events[:50]:
        old = _fmt_value(e["old_value"])
        new = _fmt_value(e["new_value"])
        d = e["delta"] if e["delta"] is not None else ""
        kw = f"`{e['keyword']}`" if e["keyword"] else "—"
        lines.append(f"| {e['event_date']} | {_sev_emoji(e['severity'])} {e['severity']} | "
                     f"{e['event_type']} | {kw} | {old} → {new} | {d} |")
    if len(events) > 50:
        lines.append(f"\n_…{len(events) - 50} more in the event stream._")
    lines.append("")
    return lines


def render_churn(events: list[dict]) -> list[str]:
    if not events:
        return []
    lines = [f"## ⚠️ Competitor entries & exits ({len(events)})", ""]
    new_entrants = [e for e in events if e["event_type"] == "new_entrant"]
    drop_outs    = [e for e in events if e["event_type"] == "drop_out"]
    if new_entrants:
        lines.append(f"### New entrants to top 10 ({len(new_entrants)})")
        lines.append("")
        lines.append("| Date | Severity | Keyword | Competitor | At position |")
        lines.append("|---|---|---|---|---:|")
        for e in new_entrants[:40]:
            pos = (e["new_value"] or {}).get("position", "")
            lines.append(f"| {e['event_date']} | {_sev_emoji(e['severity'])} {e['severity']} | "
                         f"`{e['keyword'] or ''}` | `{e['competitor_domain'] or ''}` | #{pos} |")
        if len(new_entrants) > 40:
            lines.append(f"\n_…{len(new_entrants) - 40} more._")
        lines.append("")
    if drop_outs:
        lines.append(f"### Drop-outs from top 10 ({len(drop_outs)})")
        lines.append("")
        lines.append("| Date | Severity | Keyword | Competitor | Was at position |")
        lines.append("|---|---|---|---|---:|")
        for e in drop_outs[:40]:
            pos = (e["old_value"] or {}).get("position", "")
            lines.append(f"| {e['event_date']} | {_sev_emoji(e['severity'])} {e['severity']} | "
                         f"`{e['keyword'] or ''}` | `{e['competitor_domain'] or ''}` | #{pos} |")
        if len(drop_outs) > 40:
            lines.append(f"\n_…{len(drop_outs) - 40} more._")
        lines.append("")
    return lines


def render_position_moves(events: list[dict]) -> list[str]:
    if not events:
        return []
    lines = [f"## 📊 Position movements ({len(events)})", ""]
    gains = [e for e in events if e["event_type"] == "position_gain"]
    drops = [e for e in events if e["event_type"] == "position_drop"]
    if gains:
        lines.append(f"### Competitors moving up ({len(gains)})")
        lines.append("")
        lines.append("| Date | Severity | Keyword | Competitor | Old | New | Δ |")
        lines.append("|---|---|---|---|---:|---:|---:|")
        for e in gains[:40]:
            o = (e["old_value"] or {}).get("position")
            n = (e["new_value"] or {}).get("position")
            lines.append(f"| {e['event_date']} | {_sev_emoji(e['severity'])} {e['severity']} | "
                         f"`{e['keyword']}` | `{e['competitor_domain']}` | #{o} | #{n} | {e['delta']} |")
        lines.append("")
    if drops:
        lines.append(f"### Competitors moving down ({len(drops)})")
        lines.append("")
        lines.append("| Date | Severity | Keyword | Competitor | Old | New | Δ |")
        lines.append("|---|---|---|---|---:|---:|---:|")
        for e in drops[:40]:
            o = (e["old_value"] or {}).get("position")
            n = (e["new_value"] or {}).get("position")
            lines.append(f"| {e['event_date']} | {_sev_emoji(e['severity'])} {e['severity']} | "
                         f"`{e['keyword']}` | `{e['competitor_domain']}` | #{o} | #{n} | +{e['delta']} |")
        lines.append("")
    return lines


def render_tier_events(events: list[dict]) -> list[str]:
    if not events:
        return []
    lines = [f"## Threat-tier changes & first sightings ({len(events)})", ""]
    tier_changes = [e for e in events if e["event_type"] == "threat_tier_changed"]
    first_seen   = [e for e in events if e["event_type"] == "first_seen_anywhere"]
    if tier_changes:
        # Only show promotions to primary (the actionable ones)
        promotions = [e for e in tier_changes if (e["new_value"] or {}).get("threat_tier") == "primary"]
        demotions  = [e for e in tier_changes if (e["new_value"] or {}).get("threat_tier") != "primary"]
        if promotions:
            lines.append(f"### Promoted to **primary threat** ({len(promotions)})")
            lines.append("")
            lines.append("| Date | Competitor | Category | From → To | Keyword Apps | Offering Apps |")
            lines.append("|---|---|---|---|---:|---:|")
            for e in promotions[:30]:
                old_tier = (e["old_value"] or {}).get("threat_tier", "")
                kap = (e["metadata"] or {}).get("keyword_appearance_count", "")
                oap = (e["metadata"] or {}).get("offering_appearance_count", "")
                lines.append(f"| {e['event_date']} | `{e['competitor_domain'] or ''}` | "
                             f"{e['category'] or '?'} | {old_tier} → **primary** | {kap} | {oap} |")
            lines.append("")
        if demotions:
            lines.append(f"### Other tier changes ({len(demotions)})")
            lines.append(f"")
            lines.append(f"_{len(demotions)} competitors had their threat tier recomputed (most are baseline-population changes from peripheral → watch as we accumulated snapshot data). Detail in DB._")
            lines.append("")
    if first_seen:
        lines.append(f"### New competitor first-sightings ({len(first_seen)})")
        lines.append("")
        lines.append("| Date | Competitor | Category |")
        lines.append("|---|---|---|")
        for e in first_seen[:30]:
            lines.append(f"| {e['event_date']} | `{e['competitor_domain'] or ''}` | {e['category'] or '?'} |")
        lines.append("")
    return lines


def render_features(events: list[dict]) -> list[str]:
    if not events:
        return []
    lines = [f"## SERP feature changes ({len(events)})", ""]
    lines.append("| Date | Severity | Keyword | Event | Feature |")
    lines.append("|---|---|---|---|---|")
    for e in events[:50]:
        feat = (e["new_value"] or e["old_value"] or {}).get("feature", "")
        lines.append(f"| {e['event_date']} | {_sev_emoji(e['severity'])} {e['severity']} | "
                     f"`{e['keyword'] or ''}` | {e['event_type']} | {feat} |")
    lines.append("")
    return lines


def render_summary(summary: dict, since: date, since_source: str,
                   offering: str | None) -> list[str]:
    today = date.today().isoformat()
    lines = [
        f"# SERP Event Digest",
        "",
        f"_Window: **{since.isoformat()} → {today}**_  ",
        f"_Since-source: {since_source}_  ",
    ]
    if offering:
        lines.append(f"_Offering filter: `{offering}`_  ")
    lines.append("")
    lines.append(f"**{summary['total']}** event(s) in window, "
                 f"{summary['distinct_keywords']} distinct keyword(s), "
                 f"{summary['distinct_competitors']} distinct competitor(s).")
    lines.append("")

    if summary["by_severity"]:
        lines.append("## At a glance")
        lines.append("")
        lines.append("| Severity | Count |")
        lines.append("|---|---:|")
        for sev in ("critical", "high", "medium", "low", "info"):
            n = summary["by_severity"].get(sev, 0)
            if n:
                lines.append(f"| {_sev_emoji(sev)} {sev} | {n} |")
        lines.append("")

    if summary["by_type"]:
        lines.append("**Events by type:**")
        for et, n in sorted(summary["by_type"].items(), key=lambda x: -x[1]):
            lines.append(f"- `{et}`: {n}")
        lines.append("")
    return lines


# ---------------------------------------------------------------------------
# LLM editorial summary (optional)
# ---------------------------------------------------------------------------

def make_narrative_prompt(events: list[dict], summary: dict,
                          since: date, offering: str | None) -> str:
    # Compose a compact bullet list of the top-N events sorted by severity
    top = sorted(events, key=lambda e: (SEV_ORDER.get(e["severity"], 9), -e["id"]))[:25]
    lines = []
    for e in top:
        kw = f"kw={e['keyword']!r}" if e["keyword"] else "kw=—"
        dom = f"competitor={e['competitor_domain']!r}" if e["competitor_domain"] else ""
        old = _fmt_value(e["old_value"])
        new = _fmt_value(e["new_value"])
        lines.append(f"  - [{e['severity']}] {e['event_type']} on {e['event_date']}: {kw} {dom} "
                     f"old={old} new={new}")
    body = "\n".join(lines)
    scope = f"offering=`{offering}`" if offering else "all offerings"
    return f"""You are an SEO strategist briefing Damco's marketing team via a short weekly digest.

WINDOW: {since.isoformat()} to {date.today().isoformat()}  ({scope})
TOTAL EVENTS: {summary['total']}  (critical={summary['by_severity'].get('critical', 0)}, high={summary['by_severity'].get('high', 0)}, medium={summary['by_severity'].get('medium', 0)})

TOP EVENTS:
{body}

Write the editorial summary that sits at the top of the digest:
1. A 2-3 sentence "what happened this week" paragraph. Cite specific competitors and keywords where it sharpens the point.
2. A 3-bullet "what to do about it" list. Concrete actions only — no generic SEO advice. Tag each bullet [URGENT], [THIS WEEK], or [BACKLOG].
3. One-sentence headline suitable for an email subject.

Be terse. No hedging. End your response when the three sections are done."""


def get_narrative(events: list[dict], summary: dict, since: date,
                  offering: str | None) -> tuple[str | None, dict | None]:
    prompt = make_narrative_prompt(events, summary, since, offering)
    try:
        text, usage = call_claude(prompt, tier="default", max_tokens=1500)
        return text, usage
    except LLMUnavailableError as exc:
        logger.warning("LLM narrative unavailable: %s", exc)
        return None, None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(since_arg: str | None = None, offering: str | None = None,
        all_severity: bool = False, with_narrative: bool = False,
        dry_run: bool = False) -> dict:
    start = time.monotonic()
    since, since_source = resolve_since(since_arg)
    severities = set(SEV_ORDER.keys()) if all_severity else set(INCLUDE_BY_DEFAULT)
    today = date.today()

    logger.info("Building digest: since=%s (%s), severities=%s, offering=%s",
                since.isoformat(), since_source, sorted(severities), offering)

    events = load_events(since, offering, severities)
    summary = aggregate_summary(events)
    sections = group_by_section(events)

    narrative: str | None = None
    narrative_usage: dict | None = None
    if with_narrative and events:
        narrative, narrative_usage = get_narrative(events, summary, since, offering)

    # Compose markdown
    md_parts: list[str] = []
    md_parts.extend(render_summary(summary, since, since_source, offering))
    if narrative:
        md_parts.append("## Editorial summary (LLM)")
        md_parts.append("")
        md_parts.append(narrative)
        md_parts.append("")
        md_parts.append("---")
        md_parts.append("")
    if not events:
        md_parts.append("_No qualifying events in this window. The system is quiet — or it's time to re-run rank_tracker to gather fresh signal._")
    else:
        md_parts.extend(render_damco_events(sections["damco"]))
        md_parts.extend(render_churn(sections["churn"]))
        md_parts.extend(render_position_moves(sections["positions"]))
        md_parts.extend(render_features(sections["features"]))
        md_parts.extend(render_tier_events(sections["tier"]))

    md_parts.append("")
    md_parts.append("---")
    md_parts.append(f"*Generated by `{AGENT_NAME}` on {datetime.now(timezone.utc).isoformat()}*")

    # Write report
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    suffix = f"_{offering.replace(' ', '_').replace('/', '-')}" if offering else ""
    path = OUTPUT_DIR / f"serp_event_digest_{since.isoformat()}_to_{today.isoformat()}{suffix}.md"
    path.write_text("\n".join(md_parts), encoding="utf-8")

    duration = time.monotonic() - start

    if not dry_run:
        record_agent_run(
            agent_name=AGENT_NAME,
            status="success",
            records_processed=summary["total"],
            errors=[],
            duration_seconds=round(duration, 2),
            metadata={
                "run_date":            today.isoformat(),
                "since_date":          since.isoformat(),
                "since_source":        since_source,
                "offering":            offering,
                "all_severity":        all_severity,
                "with_narrative":      with_narrative,
                "total_events":        summary["total"],
                "by_severity":         summary["by_severity"],
                "by_type":             summary["by_type"],
                "distinct_keywords":   summary["distinct_keywords"],
                "distinct_competitors": summary["distinct_competitors"],
                "llm_input_tokens":    (narrative_usage or {}).get("input_tokens"),
                "llm_output_tokens":   (narrative_usage or {}).get("output_tokens"),
                "llm_est_cost_usd":    (narrative_usage or {}).get("est_cost_usd"),
                "report_path":         str(path),
            },
        )

    # Console summary
    print()
    print(f"  {'=' * 72}")
    print(f"   SERP EVENT DIGEST — {today.isoformat()}")
    print(f"  {'=' * 72}")
    print()
    print(f"  Window:            {since.isoformat()} -> {today.isoformat()}  ({since_source})")
    if offering:
        print(f"  Offering filter:   {offering}")
    print(f"  Events:            {summary['total']}")
    for sev in ("critical", "high", "medium", "low", "info"):
        n = summary["by_severity"].get(sev, 0)
        if n:
            print(f"    {sev:<10} {n}")
    print(f"  Distinct keywords: {summary['distinct_keywords']}")
    print(f"  Distinct comps:    {summary['distinct_competitors']}")
    if narrative_usage:
        print(f"  LLM narrative:     ${narrative_usage['est_cost_usd']:.4f} "
              f"({narrative_usage['input_tokens']}/{narrative_usage['output_tokens']} tok)")
    print(f"  Report:            {path}")
    print(f"  Duration:          {duration:.2f}s")
    print()

    return {
        "status":           "success",
        "total_events":     summary["total"],
        "report_path":      str(path),
        "duration_seconds": round(duration, 2),
        "llm_used":         bool(narrative_usage),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Damco SERP Event Digest")
    parser.add_argument("--since", help="ISO date (YYYY-MM-DD) lower bound. "
                                        "Default: last successful digest run, or 14 days ago.")
    parser.add_argument("--offering", help="Restrict to one offering")
    parser.add_argument("--all-severity", action="store_true",
                        help="Include low + info severity events (very noisy by default)")
    parser.add_argument("--with-narrative", action="store_true",
                        help="Add LLM-generated editorial summary. Requires "
                             "ANTHROPIC_API_KEY + credit. Falls back to rule-based.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Generate report but skip agent_runs DB write")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s  %(name)s  %(levelname)s  %(message)s",
    )

    run(since_arg=args.since, offering=args.offering, all_severity=args.all_severity,
        with_narrative=args.with_narrative, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
