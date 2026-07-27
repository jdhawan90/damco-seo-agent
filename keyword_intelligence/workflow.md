# Keyword Intelligence — Workflow Runbook

This is the **authoritative runbook** for the Keyword Intelligence Agent. When invoked, find the section matching the user's intent and execute it exactly. Do not improvise.

All commands run from the repo root (`damco-seo-agents/`). Python is not on `PATH` on the Windows machine — the interpreter is `C:/Users/jatind1/AppData/Local/Python/pythoncore-3.14-64/python.exe`. On other machines use whatever is in `PATH`.

**Prerequisite — the tenant profile.** Client identity lives in the `tenant*` tables (migration 012), not in the code. Apply migrations before the first run on any database:

```bash
python sql/migrate.py
```

Without a tenant row every module here raises `TenantNotConfigured`. `rank_tracker` and `trend_scout` read `profile().brand_name` while building their argparse description, so even `--help` fails if the database is unreachable. If a command dies with `TenantNotConfigured`, run the migration — do not add a default.

---

## Decision tree: which workflow runs

| User says / asks | Workflow section |
|---|---|
| "run the tracker", "update rankings", "refresh keywords" | [1. Full tracking run](#1-full-tracking-run) |
| "track AI keywords", "run it for [executive]", "just for BPM" | [2. Scoped tracking run](#2-scoped-tracking-run) |
| "what's moved", "generate the report", "send the Excel" | [3. Generate report](#3-generate-report) |
| "GSC data only", "refresh GSC", "pull GSC for the last N days" | [4. GSC refresh only](#4-gsc-refresh-only) |
| "show me striking distance", "which keywords are close to top 10" | [5. Query: striking distance](#5-query-striking-distance) |
| "how is [executive] doing", "show [name]'s keywords" | [6. Query: executive performance](#6-query-executive-performance) |
| "show recent runs", "what's the last run", "is anything broken" | [7. Query: agent run health](#7-query-agent-run-health) |
| "what agents exist", "which ones use AI", "when did X last run" | `python -m common.agents` (see [section 7](#7-query-agent-run-health)) |
| "dry run", "what would happen if", "test without writing" | [8. Dry run](#8-dry-run) |
| User provides an Excel and asks to import keywords | [9. Ad-hoc data import](#9-ad-hoc-data-import) |
| "find new keywords", "what's trending", "new buzz phrases", "what is the industry talking about", "emerging terms" | [10. Trend discovery](#10-trend-discovery) |
| "review the candidates", "approve these keywords", "add the trending ones to tracking" | [11. Review and promote candidates](#11-review-and-promote-candidates) |
| "a feed is broken", "add a source", "stop polling X" | [12. Maintain the source registry](#12-maintain-the-source-registry) |
| Anything else | Ask one clarifying question, then map to the closest section above |

---

## 1. Full tracking run

**When:** user asks to track rankings, refresh the database, or doesn't specify scope.

**Cost check:** 2,126 active keywords × $0.00465 = **~$9.89** on the standard queue; ~$25.51 on live. That is well over the $1 confirmation threshold, so **a forced full run always needs the user's explicit go-ahead.** If the user hasn't specified a queue, use **standard**. Default cadence is fortnightly — the plain command (no `--all`) filters to keywords whose latest snapshot is older than `keywords.snapshot_frequency_days` (default 14), which is normally a small fraction of the 2,126 and correspondingly cheap. Count what is actually due before quoting a number.

**Steps:**

1. Confirm scope and queue with the user if not already clear. If they just said "run it", assume all active keywords on the standard queue, fortnightly-due only.
2. Execute:
   ```bash
   python -m keyword_intelligence.rank_tracker
   ```
   Flags (this is the complete list — `--help` confirms it):
   - `--offering "AI"` — restrict to one offering (still respects cadence)
   - `--all` — force every active keyword regardless of last snapshot date (use sparingly; a full forced run on 2,126 keywords costs ~$9.89)
   - `--queue live` — synchronous SERP fetch, ~2.6x cost
   - `--dry-run` — call DataForSEO but skip all DB writes
   - `--no-llm` — skip the competitor categorization pass at the end; new competitors stay `category = NULL` for human review. Rankings are unaffected.
   - `--drain-ready` — recovery mode: pull already-completed tasks from DataForSEO's ready queue. No new `task_post`, no new spend. Use this after a polling timeout left paid-for tasks unfetched. Honours `--offering`.
   - `--skip-gsc` — skip the GSC enrichment step at the end
   - `--gsc-days N` — GSC lookback window (default 14)
   - `--verbose` / `-v` — debug logging
3. The command prints:
   - Batch progress (~22 batches of 100 keywords on a forced full run)
   - Bucket distribution (1-5, 5-10, 10-20, 20-50, 50+, not-found)
   - Per-keyword position and the matched owned domain
   - Striking distance list (positions 11–20)
   - `Competitors categorized: N` — only when the pass resolved something
   - Summary totals
4. **Competition tracking write contract** — for every keyword queried, the tracker also writes:
   - One row to `keyword_serp_snapshots` (SERP-level context: AI Overview presence + cited domains, SERP features, damco position, top 10 array)
   - Up to 10 rows to `competitor_rankings` (one per top-10 result with `url_title`, `page_type`, `serp_features_owned`, `is_new_entrant`, `previous_position`, `position_change`)
   - Upserts into `competitors` for every domain seen — new domains get a stub row with `first_seen_date = today`; seen-before domains get `last_seen_date` updated
   - Calls `recompute_competitor_aggregates(competitor_id)` for every touched competitor — this updates `keyword_appearance_count`, `offering_appearance_count`, `threat_tier` and emits `threat_tier_changed` events when the tier flips
   - Diff vs previous snapshot → emits events to `competitor_serp_events` (`new_entrant`, `drop_out`, `position_gain`, `position_drop`, `damco_*`, `serp_feature_*`)
   - At end of cycle: `REFRESH MATERIALIZED VIEW CONCURRENTLY mv_offering_competition` (must run outside the snapshot transaction)

   The `device` written to `keyword_serp_snapshots` is `settings.DATAFORSEO_DEVICE` (default `desktop`), and the cadence query reads back the same value. It used to be a `'desktop'` literal in three SQL statements while the setting sat unreferenced — a mobile-first client would have been silently served desktop data. Device is part of the snapshot key, so switching it starts a parallel history.
5. **Competitor categorization** runs last, after the view refresh. One batched cheap-tier call over up to 100 domains where `competitors.category IS NULL`, ordered by `keyword_appearance_count`. It writes only into NULL rows — human curation is never overwritten — and any answer outside `direct / adjacent / big_tech / aggregator / informational / unrelated` is discarded rather than guessed. The whole step is wrapped in a try/except: it is advisory enrichment and must never cost you the ranking data already written. Suppress it with `--no-llm`.
6. GSC enrichment runs automatically at the end (14-day lookback, or `--gsc-days`). It prints its own summary.
7. After completion, verify the agent run was logged:
   ```bash
   python -c "import sys; sys.path.insert(0, '.'); from common.database import fetch_all
   for r in fetch_all('SELECT agent_name, status, records_processed, run_date, metadata FROM agent_runs ORDER BY run_date DESC LIMIT 2'):
       print(r)"
   ```
8. Report back to user:
   - Total keywords tracked
   - Brand found / not found split
   - New striking distance keywords (if any)
   - Competition-side highlights: count of `new_entrant`, `position_gain`, `damco_*` events at severity ≥ medium
   - Competitors categorized this run, if any
   - Any errors from either phase

**Failure modes:**

- **`TenantNotConfigured`** → no tenant row. Run `python sql/migrate.py`. Nothing was spent.
- **DataForSEO auth fails** → check `DATAFORSEO_LOGIN` / `DATAFORSEO_PASSWORD` in `.env`. Tell the user; don't retry.
- **GSC fails but DataForSEO succeeded** → expected when OAuth token expired. DataForSEO results are saved. Tell the user to re-run GSC once fixed (see section 4).
- **A single batch fails** → other batches succeed; affected keywords get `error` entries. Run status becomes `partial`.
- **Polling timed out after `task_post`** → the tasks are paid for and sitting in DataForSEO's ready queue. Recover with `python -m keyword_intelligence.rank_tracker --drain-ready` — no new spend. Do **not** re-run the tracker normally; that pays twice.
- **Competitor categorization unavailable** (no Anthropic credit, no key) → logged at INFO, domains stay NULL, run still succeeds. Not a failure worth reporting as one.

---

## 2. Scoped tracking run

**When:** user wants a subset — a single offering, an executive, or a specific keyword list.

**Steps:**

- **By offering** (e.g., "run for AI"):
  ```bash
  python -m keyword_intelligence.rank_tracker --offering "AI"
  ```
  The offering name must match an existing value in `keywords.offering`. Valid offerings can be listed with:
  ```bash
  python -c "import sys; sys.path.insert(0, '.'); from common.database import fetch_all
  for r in fetch_all('SELECT offering, count(*) FROM keywords WHERE status = %s GROUP BY offering ORDER BY offering', [\"active\"]):
      print(r)"
  ```
  The canonical list is the profile's — `python -c "import sys; sys.path.insert(0,'.'); from common.tenant import profile; print(profile().offering_names)"` (15 offerings). If a value in `keywords.offering` isn't in that tuple, the two have drifted; say so rather than papering over it.

- **By executive** (e.g., "run for Khushbu"): the tracker doesn't have an `--executive` flag directly. Two options:
  1. Identify the executive's offerings and run per-offering (fastest).
  2. Run the full tracker; executive-level filtering is a reporting concern.

Confirm the user's preference before choosing.

---

## 3. Generate report

**When:** user wants the Excel deliverable, asks about movement, or says "send the report".

**Steps:**

1. Generate the report:
   ```bash
   python -m keyword_intelligence.reports
   ```
   Optional flags (the complete list):
   - `--offering "AI"` — filter to one offering
   - `--start 2026-04-01 --end 2026-04-17` — restrict the date range
   - `--output path/to/file.xlsx` / `-o` — custom output path
   - `--no-narrative` — skip the Executive Summary sheet entirely (no LLM call)

2. The file is saved under `outputs/reports/ranking_report_<date>.xlsx`. It has **6 sheets** (5 with `--no-narrative`):
   - **Summary** — bucket distribution per snapshot
   - **Executive Summary** — prose interpretation, sheet position 2
   - **Detailed Rankings** — wide-format keyword × date + GSC columns (Avg Pos, Clicks, Impressions, CTR)
   - **Movement** — gains/drops between the two most recent snapshots
   - **Striking Distance** — positions 11–20 in the latest snapshot
   - **GSC Performance** — GSC metrics with SERP-vs-GSC gap analysis

   The command prints the actual sheet list it wrote — read that rather than assuming.

3. **How the Executive Summary works, and what it is allowed to say.** `compute_narrative_facts()` computes every figure in Python: bucket counts, improved/declined counts, top 10 movers each way, striking-distance count, average position per offering, and the count of keywords where the SERP snapshot and GSC's average differ by 5+ positions. Only those finished aggregates go to the model, which is instructed to use no other numbers and to do no arithmetic. **The model never sees a ranking row.** That boundary is deliberate: these are the numbers executives reconcile against last month's workbook.

   If the model is unavailable, the sheet falls back to `_rule_based_narrative()` — a real deterministic summary, not a placeholder. The sheet footer records which one produced the text (`Source: rule-based` or `Source: Claude (<model>)`). **Check that footer before quoting the summary to anyone**; while the Anthropic balance is exhausted it will say rule-based.

4. Tell the user the file path and highlight the most interesting 3–5 findings (biggest mover, new striking distance entries, high-impression low-CTR keywords).

**Prerequisites:** there must be at least one ranking snapshot in `keyword_rankings`. If the table is empty, tell the user to run section 1 first. `reports` writes no `agent_runs` row — it is a pure renderer, so don't go looking for one afterwards.

---

## 4. GSC refresh only

**When:** user wants to re-pull GSC data without re-querying DataForSEO (cheaper, no API cost).

**Steps:**

```bash
python -m keyword_intelligence.gsc_enrichment
```

Optional:
- `--days 30` — change the lookback window (default 14)
- `--dry-run` — fetch but don't write
- `-v` — verbose matching logs

**Output:**
- GSC queries returned (expect 10k–20k for the primary property)
- Matched vs. not-matched keyword counts, plus an `of which long-tail` line — how many matches came from the fallback rather than an exact query match
- Per-keyword table: keyword | GSC position | clicks | impressions | CTR

**How matching works.** Exact query match first. Failing that, the fallback finds GSC queries that contain the **whole tracked keyword** on word boundaries, and among those picks the **shortest** query (ties broken by impressions).

Two things were wrong before and both inflated the numbers:
- Matching was bidirectional, so a *broader* GSC query could claim a narrower tracked keyword — tracked "crm for insurance" would take the metrics for the query "crm".
- It was raw substring, so "crm" also matched "scrm" and "crmsoftware".

Directional plus word-boundary now, and closest-match rather than highest-impression, because `max(impressions)` systematically picked the most generic variant. **Expect the match count to be lower than historical runs.** That is the fix working, not a regression — don't chase it.

`gsc_enrichment` deliberately never calls a model. A non-deterministic match would jitter position history for reasons unrelated to ranking.

**GSC data lag:** Google reports a ~3-day lag. A 14-day run actually covers `today - 17 days` to `today - 3 days`. This is by design.

---

## 5. Query: striking distance

**When:** user asks "what's close to top 10", "striking distance", "which should we push".

Run this SQL against the DB (use the `common.database.fetch_all` helper):

```sql
SELECT k.keyword, k.offering, e.name AS executive,
       kr.rank_position AS serp_rank,
       gsc.rank_position AS gsc_avg_pos,
       gsc.clicks, gsc.impressions, gsc.ctr
FROM keyword_rankings kr
JOIN keywords k ON k.id = kr.keyword_id
LEFT JOIN keyword_rankings gsc ON gsc.keyword_id = k.id
     AND gsc.source = 'gsc'
     AND gsc.date = (SELECT max(date) FROM keyword_rankings WHERE keyword_id = k.id AND source = 'gsc')
LEFT JOIN executive_keyword_assignments a ON a.keyword_id = k.id
LEFT JOIN seo_executives e ON e.id = a.executive_id
WHERE kr.source != 'gsc'
  AND kr.date = (SELECT max(date) FROM keyword_rankings WHERE keyword_id = k.id AND source != 'gsc')
  AND kr.rank_position BETWEEN 11 AND 20
ORDER BY gsc.impressions DESC NULLS LAST, kr.rank_position;
```

Present as a table: keyword, offering, executive, SERP rank, GSC avg, clicks, impressions. Sort by GSC impressions descending (biggest opportunity first).

---

## 6. Query: executive performance

**When:** user asks "how is Khushbu doing", "show Ekta's keywords", "executive breakdown".

Pick the right query depending on what's asked:

- **Summary per executive:**
  ```sql
  SELECT e.name,
         count(DISTINCT k.id) AS total_keywords,
         count(DISTINCT k.id) FILTER (WHERE kr.rank_position <= 10) AS top_10,
         count(DISTINCT k.id) FILTER (WHERE kr.rank_position BETWEEN 11 AND 20) AS striking,
         count(DISTINCT k.id) FILTER (WHERE kr.rank_position IS NULL) AS not_found
  FROM seo_executives e
  JOIN executive_keyword_assignments a ON a.executive_id = e.id
  JOIN keywords k ON k.id = a.keyword_id
  LEFT JOIN keyword_rankings kr ON kr.keyword_id = k.id
       AND kr.source != 'gsc'
       AND kr.date = (SELECT max(date) FROM keyword_rankings WHERE keyword_id = k.id AND source != 'gsc')
  GROUP BY e.name ORDER BY e.name;
  ```

- **Specific executive's detailed keywords:**
  ```sql
  SELECT k.keyword, k.offering, k.services,
         kr.rank_position AS serp, gsc.rank_position AS gsc_avg, gsc.impressions
  FROM keywords k
  JOIN executive_keyword_assignments a ON a.keyword_id = k.id
  JOIN seo_executives e ON e.id = a.executive_id
  LEFT JOIN keyword_rankings kr ON kr.keyword_id = k.id AND kr.source != 'gsc'
       AND kr.date = (SELECT max(date) FROM keyword_rankings WHERE keyword_id = k.id AND source != 'gsc')
  LEFT JOIN keyword_rankings gsc ON gsc.keyword_id = k.id AND gsc.source = 'gsc'
       AND gsc.date = (SELECT max(date) FROM keyword_rankings WHERE keyword_id = k.id AND source = 'gsc')
  WHERE e.name = %s
  ORDER BY gsc.impressions DESC NULLS LAST, k.keyword;
  ```
  Parameterize with the executive name.

---

## 7. Query: agent run health

**When:** user asks about run status, recent runs, or if anything is broken.

```sql
SELECT agent_name, status, records_processed, duration_seconds,
       run_date, metadata
FROM agent_runs
WHERE agent_name LIKE 'keyword_intelligence.%'
ORDER BY run_date DESC
LIMIT 10;
```

Present: last 10 runs with status, records, duration. Highlight any `error` or `partial` statuses. If the last run is older than 2 weeks, mention that tracking may be stale.

**For "what agents exist" / "when did X last run" across the whole system, use the registry rather than any prose table in a CLAUDE.md:**

```bash
python -m common.agents                       # all 22 agents, folder-grouped, with last-run status
python -m common.agents --folder keyword_intelligence
python -m common.agents --validate            # catalogue vs. filesystem
python -m common.agents --json                # machine-readable
```

This is the honest answer — it reads `agent_runs`, so it cannot drift the way a hand-maintained status table does. Note that `keyword_intelligence.reports` shows `never`: it logs no `agent_runs` row by design.

---

## 8. Dry run

**When:** user wants to see what would happen without writing to the DB.

```bash
python -m keyword_intelligence.rank_tracker --dry-run
```

- DataForSEO calls still happen (costs real money) unless you skip with `--skip-gsc --dry-run` combined with offering filtering.
- Cadence filter still applies — `--dry-run` only queries keywords that are due. Combine with `--all` to force-fetch every active keyword without writing.
- Nothing is written to `keyword_rankings` or `agent_runs`.
- Useful for validating keyword coverage and brand matching before committing.

Warn the user that dry run still incurs API cost.

---

## 9. Ad-hoc data import

**When:** user provides a spreadsheet of keywords to add or update.

**Do not commit the import script.** Write it inline, run it, verify results, then delete it. The repo stays focused on agent code, not data loading.

### Pre-import checklist (do this BEFORE writing the import code)

Skipping any of these has caused real data problems. Each item traces to a specific past mistake.

1. **List all sheet names with `repr()` — watch for trailing whitespace.**
   ```python
   print('Sheets:', [repr(s) for s in wb.sheetnames])
   ```
   Real case: a file had `'Overall Rankings '` (trailing space) and `'Sheet1'`. Getting the wrong sheet silently produced bad columns. Use `repr()` so whitespace is visible.

2. **Dump the header row cell-by-cell to verify column positions.** Do not trust `ws.iter_rows()` output from an exploratory script — it can skip empty cells and mislead you about which column holds what.
   ```python
   for cell in ws[header_row]:
       print(f'  col {cell.column} ({cell.column_letter}): {cell.value!r}')
   ```

3. **Compare sheets in the same file carefully.** A mastersheet may have multiple sheets with similar-looking headers. The "summary" sheet often has fewer columns than the "detail" sheet. Confirm with the user which sheet is the source of truth before importing.
   - Example: one file had `Overall Rankings ` (4 columns: Category, Priority, SEO Member, Keywords — 166 rows, the master) and `Sheet1` (10+ columns including search volume, intent, ranking history — 511 rows, a superset of candidates). The sheet with FEWER columns was the one to use.

4. **Identify ALL columns that have merged cells for fill-down.** In a master sheet, the executive and priority usually span an entire category block — only the first row has the value, subsequent rows rely on the merge. If you only fill-down one column (e.g., only Category) you will lose 80%+ of the executive assignments.
   ```python
   last_category = last_priority = last_executive = None
   for row_idx in range(header_row + 1, ws.max_row + 1):
       cat = ws.cell(row_idx, cat_col).value
       pri = ws.cell(row_idx, pri_col).value
       mem = ws.cell(row_idx, mem_col).value
       kw = ws.cell(row_idx, kw_col).value
       if cat: last_category = str(cat).strip()
       if pri: last_priority = str(pri).strip()
       if mem: last_executive = str(mem).strip().title()
       if not kw: continue
       # ... now use last_category, last_priority, last_executive
   ```

5. **Always dry-run the extraction before writing to the DB.** Print `len(keywords_data)` and a breakdown by executive/category. Compare with the user's expected count. If they disagree, **stop and ask** before proceeding.

6. **Confirm the expected count with the user.** "I see 166 keywords, 104 for Himanshu, 62 for Gunjan. Proceed?" — cheap, prevents expensive rework.

### Template for inline import

```python
# Run this as a one-off — do not save as a .py file in the repo
import sys; sys.path.insert(0, '.')
import openpyxl
from common.database import connection, fetch_all

FILE = "path/to/file.xlsx"
SHEET = "SheetName"   # copy-paste from repr() output — may contain whitespace
OFFERING = "OfferingName"
ALLOWED_EXECS = {"ExecA", "ExecB"}  # if filtering is required

wb = openpyxl.load_workbook(FILE, data_only=True)
ws = wb[SHEET]
header = {str(c.value).strip(): c.column for c in ws[HEADER_ROW] if c.value}

keywords_data = []
last_cat = last_pri = last_exec = None
for row_idx in range(HEADER_ROW + 1, ws.max_row + 1):
    cat = ws.cell(row_idx, header['Category']).value
    pri = ws.cell(row_idx, header['Page Priority']).value
    mem = ws.cell(row_idx, header['SEO Member']).value
    kw  = ws.cell(row_idx, header['Keywords']).value
    if cat: last_cat = str(cat).strip()
    if pri: last_pri = str(pri).strip()
    if mem: last_exec = str(mem).strip().title()
    if not kw or not str(kw).strip():
        continue
    if ALLOWED_EXECS and last_exec not in ALLOWED_EXECS:
        continue
    importance = last_pri.lower() if last_pri and last_pri.lower() in ('high','medium','low') else 'medium'
    keywords_data.append({
        'keyword': str(kw).strip().lower(),
        'services': last_cat,
        'importance': importance,
        'executive': last_exec,
    })

# Print summary — VERIFY with the user before inserting
exec_counts = {}
for kw in keywords_data:
    exec_counts[kw['executive']] = exec_counts.get(kw['executive'], 0) + 1
print(f'Extracted: {len(keywords_data)} — by exec: {exec_counts}')

# (pause here — confirm with user if counts don't match expectation)

# Upsert into database
with connection() as conn:
    with conn.cursor() as cur:
        for name in {kw['executive'] for kw in keywords_data}:
            cur.execute("INSERT INTO seo_executives (name) VALUES (%s) "
                        "ON CONFLICT (name) DO UPDATE SET name = EXCLUDED.name RETURNING id", (name,))
        # then insert keywords + assignments as in the first example
```

### Re-import / cleanup

If a previous import was wrong, delete it explicitly before re-importing:

```sql
DELETE FROM executive_keyword_assignments
    WHERE keyword_id IN (SELECT id FROM keywords WHERE offering = %s);
DELETE FROM keyword_search_volume
    WHERE keyword_id IN (SELECT id FROM keywords WHERE offering = %s);
DELETE FROM keywords WHERE offering = %s;
```

Never rely on "it'll get overwritten on re-import" — ON CONFLICT only handles matching keys, not orphaned rows from previous wrong data.

### After import

1. Verify row counts with a `SELECT count(*)` query.
2. Summarize what was added (by offering, by executive).
3. Tell the user, but don't commit the loader.

---

## 10. Trend discovery

**When:** the user asks what's trending, wants new keyword ideas, or asks what the industry is talking about that we don't track.

**What it does:** polls the ~41 enabled feeds in `trend_sources` (tech press, practitioner communities, Medium tag feeds), extracts recurring phrases, drops what we already track, classifies each into an offering, prices it against Google Ads Keyword Planner, and scores it. Results land in `keyword_candidates` — **never** in `keywords`.

The offering vocabulary, the commercial tokens and the generic-head news nouns all come from the tenant profile (`tenant_offerings`, `tenant_vocabularies`), cached per process. Keyword Planner is now queried with the profile's `location_code` / `language_code` rather than the connector default. To retune classification, edit those tables — not this module.

**Cost:** ~$0.05 per run (one Keyword Planner batch covers up to 1,000 keywords) plus a few cents of Claude Haiku classification. Feeds are free. Voyage embeddings are rounding error — $0.02 per million tokens on ~5-token inputs, and the tracked set is embedded once and cached. Well under the $1 confirmation threshold — just run it.

**Runtime:** ~4-5 minutes. Most of it is polite rate limiting, Reddit especially (12s between subreddits, ~2.5 min for the 13 of them).

```bash
# Standard run — 14-day lookback
python -m keyword_intelligence.trend_scout

# Wider net when the last run found little
python -m keyword_intelligence.trend_scout --days 30

# Only sources tied to one offering
python -m keyword_intelligence.trend_scout --offering AI

# Free preview — no writes, no paid lookups
python -m keyword_intelligence.trend_scout --dry-run

# Skip the paid volume lookup but still write candidates
python -m keyword_intelligence.trend_scout --no-volume

# Token rules only, no Claude
python -m keyword_intelligence.trend_scout --no-llm

# Skip the Excel/markdown artifacts
python -m keyword_intelligence.trend_scout --skip-reports
```

Tuning flags, rarely needed: `--max-items` (per source, default 60), `--min-mentions` (default 2), `--min-spread` (distinct sources, default 2), `--max-volume-lookups` (Keyword Planner cap, default 600), `--verbose`.

**Cadence:** weekly or fortnightly. Daily is supported and cheap, but a 14-day window won't shift much day to day.

**Novelty is checked twice.** Token-set Jaccard runs first as a cheap prefilter during extraction. Jaccard cannot see paraphrase — "ai agent orchestration platform" and a tracked "agentic ai development services" share almost no tokens and scored as unrelated — so the survivors are re-checked against Voyage embeddings (`voyage-3-lite`, cached in `keyword_embeddings`, migration 015). One batched call per run, then arithmetic. The threshold is 0.80 similarity, and the re-check only ever makes a candidate *less* novel; a high Jaccard score is never overridden downward.

This is a spend guard, not a nicety: a promoted paraphrase costs money on every future rank-tracker run, forever.

Read the run output to know which one you got:
- `Semantic novelty:    N checked, M reclassified as near-duplicates` — embeddings ran
- `Semantic novelty:    skipped (embeddings unavailable — Jaccard result kept)` — **dormant.** `VOYAGE_API_KEY` is unset or the `voyageai` SDK is missing. The run is still valid, but near-duplicates will get through, so review the candidate list more sceptically before promoting.

**Reading the output.** Five sub-scores roll into `trend_score`:

| Component | Weight | Question it answers |
|---|---:|---|
| Buzz | 30 | Is the industry talking about it, across more than one outlet? |
| Volume | 25 | Does anyone search it? |
| Momentum | 20 | Is that demand rising? (last 3 months vs prior 9) |
| Opportunity | 15 | Is it new territory, or a rewording of something we track? |
| Commercial | 10 | Would the traffic be worth anything? |

**Momentum is the column that matters most.** A 720/mo keyword at 4.29× is a better bet than a 40,000/mo keyword at 0.9× — the first is becoming a market, the second already is one and is fully contested.

**Things that will look wrong but aren't:**

- **Most candidates are AI.** That's what the industry is publishing about. The "By Offering" sheet shows the distribution honestly; use `--offering` to dig into a quieter area.
- **A second run the same day finds few new mentions.** Correct — mentions are deduped by content hash. Scoring still runs over the full rolling window, so the candidate list stays stable rather than collapsing to near-empty.
- **Implausibly large search volumes** (e.g. "frontier ai" at 2.2M/mo). Keyword Planner groups close variants and this is its own number, reported faithfully. Sanity-check anything above ~500k before promoting it.
- **`suggested_offering` is sometimes wrong** on generically-worded phrases. Confidence is recorded (`offering_confidence`); rule matches score 0.75, source hints 0.50. Fix it in the review step, or in `tenant_offerings` if the token vocabulary is genuinely missing something. Not by editing code.
- **`trend_score` sits higher than it used to for rule-classified candidates.** `intent` was only ever set on LLM-classified ones, so roughly 80% of candidates could never earn the 1.15x commercial multiplier — they were systematically under-scored against whichever ones the LLM happened to handle. `_infer_intent()` now assigns intent by rule too (transactional / informational / commercial), using the same vocabulary the classifier uses, and LLM intent still wins when present. `intent` also lands in `keywords.intent` on promotion, so the old gap outlived the run. **Scores are not comparable to candidates generated before 2026-07-27.**
- **The classifier prompt doesn't name the company.** Identity goes in via `system_preamble()` as `system=`; the user prompt lists only the offerings, tenant-neutral by construction. If you see a brand name in a user prompt anywhere in this folder, that's a bug.

---

## 11. Review and promote candidates

**When:** after a discovery run, or when the user asks to add trending keywords to tracking.

**This is a human gate. Never promote without the user explicitly choosing the keywords.** Every promoted keyword adds recurring DataForSEO cost to every future rank-tracker run — 2,126 keywords already cost ~$9.90 per full run.

**Step 1 — show the queue:**

```bash
python -m keyword_intelligence.trend_scout --list-candidates --min-score 60 --limit 40
```

`--list-candidates` defaults to `status='new'`. Use `--status` to review what
you have already triaged — `approved`, `rejected`, `promoted`, `duplicate`,
`reviewed`, or `all`:

```bash
python -m keyword_intelligence.trend_scout --list-candidates --status approved
python -m keyword_intelligence.trend_scout --list-candidates --status rejected --limit 20
```

Or in SQL, for the richer view:

```sql
SELECT * FROM v_trend_review_queue LIMIT 40;
```

**Step 2 — let the user pick.** Present the list with volume and momentum. Do not pre-select for them.

**Step 3 — record the decision:**

```sql
UPDATE keyword_candidates
   SET status = 'approved', reviewed_by = '<name>', reviewed_at = now()
 WHERE id IN (2, 4, 83);

-- Rejecting is equally valuable — a rejected candidate stays rejected
-- across all future runs instead of resurfacing every week.
UPDATE keyword_candidates
   SET status = 'rejected', reviewed_by = '<name>', reviewed_at = now(),
       review_note = 'too broad / already covered by <keyword>'
 WHERE id IN (7, 13);
```

**Step 4 — promote:**

```bash
# Preview
python -m keyword_intelligence.trend_scout --promote --ids 2,4,83 --dry-run

# Execute
python -m keyword_intelligence.trend_scout --promote --ids 2,4,83
```

Promotion inserts into `keywords` with `ON CONFLICT (keyword, offering) DO NOTHING`. A candidate that already exists is marked `duplicate` and linked to the existing row — nothing is overwritten.

**Step 5 — the promoted keywords have no `target_url` and no executive.** Set both before the next tracking run:

```sql
UPDATE keywords SET target_url = '<url>' WHERE id = <id>;
INSERT INTO executive_keyword_assignments (executive_id, keyword_id) VALUES (<exec>, <id>);
```

**Step 6 — get a first ranking:**

```bash
python -m keyword_intelligence.rank_tracker --offering "<offering>" --all
```

---

## 12. Maintain the source registry

**When:** a run reports failing sources, or the user wants to add/remove a feed.

The harvester prints a warning for any enabled source with 3+ consecutive failures. Act on it — a silently dead feed becomes a blind spot in one offering while the run still reports "success".

```sql
-- Health check
SELECT name, category, last_status, consecutive_failures,
       substr(last_error, 1, 80) AS err, last_polled_at
  FROM trend_sources
 WHERE enabled AND last_status <> 'ok'
 ORDER BY consecutive_failures DESC;
```

**Reading the statuses:**

| Status | Meaning | Action |
|---|---|---|
| `ok` | Items returned | None |
| `empty` | Fetched fine, nothing inside the lookback window | Usually fine — a low-volume blog. Investigate if it persists for weeks. |
| `error` | Network failure or malformed XML | One retry already happened. Persistent = the feed moved; find the new URL. |
| `blocked` | HTTP 403/429 | 403 means the origin refuses our User-Agent — retire and replace. 429 means we're polling too fast — that's a code fix in `common/connectors/feeds.py`, not a registry fix. |

**Add a source** (no code change needed — the registry is data):

```sql
INSERT INTO trend_sources (name, url, source_type, category, offering_hint, weight)
VALUES ('Feed name', 'https://example.com/feed/', 'rss', 'tech_press', 'Cloud', 1.10);
```

- `source_type`: `rss` (also covers Atom and Medium tag feeds), `reddit`, `hackernews`
- `category`: `tech_press`, `community`, `blog_platform`, `vendor_blog` — drives the category-spread bonus in scoring
- `offering_hint`: optional; biases classification when token rules can't place a phrase
- `weight`: 0.5–1.5 editorial trust multiplier

**Verify a new URL before inserting it.** A feed that answers with HTML is reported as an error, but it's cheaper to check first than to discover it a week later.

**Retire a source** — disable, don't delete. `trend_mentions` references it, and the evidence behind already-scored candidates should stay readable:

```sql
UPDATE trend_sources SET enabled = FALSE, last_error = '<why>' WHERE name = '...';
```

---

## What to always do after any workflow

1. **Always show the result**, not just "done" — numbers, keywords, filepaths.
2. **Suggest a logical next step** if obvious (e.g., after a tracking run, suggest generating the report).
3. **Log to agent_runs** — `rank_tracker.py`, `gsc_enrichment.py` and `trend_scout.py` do this automatically. `reports.py` does not; it is a renderer, not an agent run. For ad-hoc DB work, consider whether a custom `agent_runs` entry helps audit trail.
4. **Never claim success without verification** — always read back at least one row of what was written.
5. **Say which path produced any generated prose.** The Executive Summary sheet and the competitor categories both degrade silently to their non-LLM fallback. Reporting model output when the rule-based fallback actually ran is how these runbooks went stale in the first place.
