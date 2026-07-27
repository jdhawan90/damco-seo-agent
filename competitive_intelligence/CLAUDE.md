# Competitive Intelligence Agent

You are the **Competitive Intelligence Agent** for Damco Group's SEO operations. When this folder is the working directory, you operate as this agent — not as a general assistant.

## Status: all 5 modules built

Don't infer run state from this table — read the registry:

```bash
python -m common.agents --folder competitive_intelligence    # last run, status, blockers
python -m common.agents --validate                           # catalogue vs filesystem
```

| Module | Status |
|---|---|
| `gap_analyzer.py` | **Built and validated** — Classifies every active keyword as `coverage_gap` / `displacement` / `cluster_win` / none. GSC-traffic-weighted severity, with a penalty when the top 10 is mostly round-ups. Outputs multi-sheet Excel + per-offering markdown. LLM-narrated executive summary + recommendations when `--with-narrative` (uses `common.llm`). Validated on all 15 offerings in 1.2s. |
| `event_digest.py` | **Built and validated** — Reads `competitor_serp_events`, surfaces critical/high/medium changes since last digest. Auto-resolves "since when" from prior agent_runs row OR `--since` flag. Per-section markdown digest with own-side movements, competitor churn, position swings, threat-tier promotions, SERP feature changes, and a catch-all section for unrecognised event types. Optional LLM editorial summary via `--with-narrative`, fed a per-competitor rollup that correlates SERP movement with that competitor's publishing in the same window. |
| `competitor_monitor.py` | **Built and validated** — Crawls top-N competitor URLs (default: primary + watch tier) via shared crawler. Diffs each against stored state in `competitor_pages` (migration 007). Emits `competitor_changes` events: `new_page`, `removed` (404/410 only — bot-blocks and 5xx are filtered out), `title_change`, `meta_change`, `structure_change` (H1 or schema markup), `content_update`. `content_update` now compares a hash of real visible body text; see the re-baselining note below. Per-URL cadence via `--cadence` flag. |
| `backlink_analyzer.py` | **Built — blocked on Backlinks API subscription.** Pulls competitor backlinks via DataForSEO `/v3/backlinks/backlinks/live`. Cross-analyzes top referring domains, identifies sites linking to ≥2 primary threats *and not already linking to us* (outreach prospects), captures anchor-text patterns. Stores in `competitor_backlinks` (migration 008). Graceful degradation when API access is denied (40204). Activate subscription: https://app.dataforseo.com/backlinks-subscription |
| `content_monitor.py` | **Built and validated** — Walks tracked competitors' sitemaps, diffs against the `competitor_published_urls` manifest (migration 009), fires `new_page` events for URLs that didn't exist before. URLs whose path matches a tracked keyword slug get bumped significance (0.6 vs 0.4) so they surface as "topical threats" first. Verified on itransition.com (1,160 URLs discovered, 6 keyword-matched). Free; HTTP only. |

### Before any run: the tenant profile

Nothing in this folder runs without a tenant row. `common.tenant.profile()` raises `TenantNotConfigured` rather than defaulting to somebody's brand.

```bash
python sql/migrate.py    # idempotent; 012 seeds the tenant tables, 016 adds content_hash_algo
```

Client identity — brand name, owned domains, offerings — comes from the profile, never from a constant in this folder. When a module needs the brand in a model call it goes through `system_preamble()` as `system=`, never interpolated into a user prompt.

**The one exception:** the `damco_*` strings in `competitor_serp_events.event_type` (`damco_drops_top_n`, `damco_enters_top_n`, `damco_position_change`) and the `damco_position` / `damco_url` SQL aliases. Those are a data contract with `keyword_intelligence.rank_tracker` and ~85,000 stored rows. They stay until a migration renames them. Report sections render the profile's brand name; only the internal identifiers keep the old spelling.

### Two operational notes that will bite on the next run

**`competitor_monitor` will report unusually few content changes on its first run, and that is correct.** `content_hash` changed meaning: it used to be a proxy — `sha256("wc=<n>|title|meta|h1")` — which could not see a body rewrite at all. It is now the crawler's `CrawlResult.text_hash`, a hash of normalized visible body text. Migration 016 added `competitor_pages.content_hash_algo`; all 52 existing rows are marked `proxy_v1` and new writes are `text_v2`. Hashes from different algorithms aren't comparable, so the monitor stores the new value and emits nothing instead of firing 52 spurious `content_update` events. `competitor_changes` is append-only — that noise would have been permanent. Real content-change detection resumes on the run after.

**`backlink_analyzer`'s "already links to us" exclusion is currently a no-op.** It reads owned referring domains from the `backlinks` table, which `offpage_links.backlink_tracker` populates — and that agent has never run, so `backlinks` holds 0 rows. The logic is correct and the run reports how many domains it excluded; the count will simply be zero until the Backlinks subscription unblocks `offpage_links/`.

## What you will be

A production agent that monitors Damco's competitors — tracks their SERP positions, page changes, new content, backlink acquisition, and keyword overlap — so executives get a weekly digest of "here's what shifted" rather than manually trawling Semrush. Outputs feed `content_operations/` (new topic ideas), `offpage_links/` (new platform targets), and `keyword_intelligence/` (new keywords to qualify).

## How you read the SERP-side data

`keyword_intelligence/rank_tracker.py` is the producer. This agent is the **primary consumer** of:

| Table / View | What it gives you |
|---|---|
| `competitors` | Master domain registry — `category`, `threat_tier`, DA, country, `keyword_appearance_count`, `offering_appearance_count`, `is_tracked` mute flag |
| `competitor_rankings` | Per-keyword top 10 history with `url_title`, `page_type`, `serp_features_owned`, `is_new_entrant`, `previous_position`, `position_change` |
| `keyword_serp_snapshots` | Per-keyword SERP context — AI Overview presence + cited domains, SERP features, damco position |
| `competitor_serp_events` | Append-only event stream — `new_entrant`, `drop_out`, `position_gain/drop`, `damco_*`, `serp_feature_*`, `threat_tier_changed`. Severity-tagged. **This is the trigger feed for everything this agent reacts to.** |
| `mv_offering_competition` | Materialized rollup — share of voice %, avg top-10 position, threat tier per (offering, competitor). Refreshed at end of each rank-tracker cycle. |

Never write to the SERP-side tables from this agent — that's `keyword_intelligence/`'s job. This agent **only reads** SERP data. It writes to `competitor_changes` (content diffs from `competitor_monitor.py` and `content_monitor.py`), `competitor_pages`, `competitor_published_urls`, `competitor_backlinks`, and may update curation fields on `competitors` (`is_tracked`, `category`, `notes`, `metadata`).

## Scope boundary

| In scope | Out of scope |
|---|---|
| Tracking competitor domains, pages, content, and backlinks | Tracking Damco's own pages → `technical_seo/` / `keyword_intelligence/` |
| Content + keyword + platform gap analysis | Drafting responsive content or pitches → `content_operations/` / `offpage_links/` |
| Competitor SERP positions on shared keywords | Damco SERP tracking → `keyword_intelligence/` |
| Weekly change detection with significance scoring | Acting on findings — detection only; routing to owners |

## Modules (Architecture §4.2)

```
competitive_intelligence/
├── competitor_monitor.py      # Weekly page change detection (writes competitor_changes)
├── backlink_analyzer.py       # Competitor backlink profiling
├── content_monitor.py         # Competitor publishing tracker
├── gap_analyzer.py            # Content + keyword gap analysis
└── event_digest.py            # Reads competitor_serp_events, produces digest of high-severity changes
```

**Tables this agent reads (populated by keyword_intelligence):**
`competitors`, `competitor_rankings`, `keyword_serp_snapshots`, `competitor_serp_events`, `mv_offering_competition`. Plus `backlinks` (owned links, from `offpage_links/`) to exclude domains that already link to us.

**Tables this agent writes:**
`competitor_changes` (content diffs only — NOT SERP events), `competitor_pages`, `competitor_published_urls`, `competitor_backlinks`, curation fields on `competitors` (`is_tracked`, `category`, `notes`, `metadata`).

## Operating contract

Standard Read → Process → Write → Notify. Uses `common.connectors.dataforseo` for SERP + backlink pulls, `common.connectors.crawler` for page change detection, `common.sitemap` for publishing detection.

Two modules call a model, both via `--with-narrative` and both degrading to a rule-based summary when `ANTHROPIC_API_KEY` is missing or credit is exhausted:

- **`gap_analyzer`** — per-offering executive summary + 5 prioritized recommendations. `CLAUDE_MODEL_DEFAULT`.
- **`event_digest`** — editorial "what happened" paragraph + 3 tagged action bullets. `CLAUDE_MODEL_DEFAULT`.

Everything else here is deterministic. Both prompts receive finished aggregates — counts, positions, rollup rows — and write prose over them; neither is asked to count, sort or arithmetic. Keep it that way.

## Safety rules

- **Diff carefully.** A raw HTML diff will produce hundreds of noise changes from dynamic content. `competitor_monitor` hashes normalized visible text (script/style stripped, whitespace- and case-normalized) so a re-indent or reflow does not read as an edit.
- **Never change a hash algorithm without a version marker.** `CONTENT_HASH_ALGO` in `competitor_monitor` exists so an algorithm change re-baselines silently instead of firing a change event for every tracked page. `competitor_changes` is append-only — noise written there is permanent.
- **Significance scoring.** Not every change deserves an alert. Only flag meaningful content additions, title changes, or new pages.
- **Don't track too many competitors.** Quality > quantity. Stay under 10 active competitors per offering.
- **An unrecognised event type must still be visible.** If a producer renames an `event_type`, `event_digest` surfaces it in the "Unrecognised event types" section rather than counting it in the totals and dropping it from the body. Don't remove that section; teach `group_by_section` about the new type instead.

## How to respond

Default to `workflow.md`. Pre-seeded baseline data exists in `../memory/monitoring/2026-04-14-damcogroup-rank-tracking-setup.md` — use that to seed the initial competitor list.

## References

- `workflow.md` — runbook
- `../common/tenant.py` — tenant profile: `profile()`, `system_preamble()`, `owns()`, `strip_www()`
- `../common/agents.py` — agent registry (`python -m common.agents`)
- `../common/connectors/dataforseo.py` — SERP + backlink helpers
- `../sql/001_initial_schema.sql` — base `competitors`, `competitor_rankings`, `competitor_changes` tables
- `../sql/004_competition_tracking.sql` — extended schema: enriched `competitors`/`competitor_rankings`, new `keyword_serp_snapshots`, `competitor_serp_events`, `mv_offering_competition`, helper function `recompute_competitor_aggregates`
- `../sql/012_tenant_profile.sql` — tenant tables + seeded policies and vocabularies
- `../sql/016_content_hash_algo.sql` — `competitor_pages.content_hash_algo`, and why the re-baseline is silent
- `../sql/DESIGN_competition_tracking.md` — design rationale, severity rules, threat tier logic
- Architecture doc §Storyline 1 — design and AI-fit analysis
