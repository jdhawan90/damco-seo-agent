# Keyword Intelligence — Workflow Runbook

This is the **authoritative runbook** for the Keyword Intelligence Agent. When invoked, find the section matching the user's intent and execute it exactly. Do not improvise.

All commands run from the repo root (`damco-seo-agents/`). The Python interpreter path on the Windows machine is `C:/Users/jatind1/AppData/Local/Python/bin/python.exe` — on other machines use whatever is in `PATH`.

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
| "dry run", "what would happen if", "test without writing" | [8. Dry run](#8-dry-run) |
| User provides an Excel and asks to import keywords | [9. Ad-hoc data import](#9-ad-hoc-data-import) |
| Anything else | Ask one clarifying question, then map to the closest section above |

---

## 1. Full tracking run

**When:** user asks to track rankings, refresh the database, or doesn't specify scope.

**Cost check:** 798 keywords × $0.0006 = ~$0.48 on standard queue. Live queue is ~$1.60. If the user hasn't specified, use **standard**.

**Steps:**

1. Confirm scope and queue with the user if not already clear. If they just said "run it", assume all active keywords on the standard queue.
2. Execute:
   ```bash
   python -m keyword_intelligence.rank_tracker
   ```
3. The command prints:
   - Batch progress (7–8 batches of 100 keywords)
   - Bucket distribution (1-5, 5-10, 10-20, 20-50, 50+, not-found)
   - Per-keyword position and matched Damco domain
   - Striking distance list (positions 11–20)
   - Summary totals
4. GSC enrichment runs automatically at the end (14-day lookback). It prints its own summary.
5. After completion, verify the agent run was logged:
   ```bash
   python -c "import sys; sys.path.insert(0, '.'); from common.database import fetch_all
   for r in fetch_all('SELECT agent_name, status, records_processed, run_date, metadata FROM agent_runs ORDER BY run_date DESC LIMIT 2'):
       print(r)"
   ```
6. Report back to user:
   - Total keywords tracked
   - Brand found / not found split
   - New striking distance keywords (if any)
   - Any errors from either phase

**Failure modes:**

- **DataForSEO auth fails** → check `DATAFORSEO_LOGIN` / `DATAFORSEO_PASSWORD` in `.env`. Tell the user; don't retry.
- **GSC fails but DataForSEO succeeded** → expected when OAuth token expired. DataForSEO results are saved. Tell the user to re-run GSC once fixed (see section 4).
- **A single batch fails** → other batches succeed; affected keywords get `error` entries. Run status becomes `partial`.

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
   Optional flags:
   - `--offering "AI"` — filter to one offering
   - `--start 2026-04-01 --end 2026-04-17` — restrict the date range
   - `--output path/to/file.xlsx` — custom output path

2. The file is saved under `outputs/reports/ranking_report_<date>.xlsx`. It has 5 sheets:
   - **Summary** — bucket distribution per snapshot
   - **Detailed Rankings** — wide-format keyword × date + GSC columns (Avg Pos, Clicks, Impressions, CTR)
   - **Movement** — gains/drops between the two most recent snapshots
   - **Striking Distance** — positions 11–20 in the latest snapshot
   - **GSC Performance** — GSC metrics with SERP-vs-GSC gap analysis

3. Tell the user the file path and highlight the most interesting 3–5 findings (biggest mover, new striking distance entries, high-impression low-CTR keywords).

**Prerequisite:** there must be at least one ranking snapshot in `keyword_rankings`. If the table is empty, tell the user to run section 1 first.

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
- GSC queries returned (expect 10k–20k for damcogroup.com)
- Matched vs. not-matched keyword counts
- Per-keyword table: keyword | GSC position | clicks | impressions | CTR

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

---

## 8. Dry run

**When:** user wants to see what would happen without writing to the DB.

```bash
python -m keyword_intelligence.rank_tracker --dry-run
```

- DataForSEO calls still happen (costs real money) unless you skip with `--skip-gsc --dry-run` combined with offering filtering.
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

## What to always do after any workflow

1. **Always show the result**, not just "done" — numbers, keywords, filepaths.
2. **Suggest a logical next step** if obvious (e.g., after a tracking run, suggest generating the report).
3. **Log to agent_runs** — `rank_tracker.py` and `gsc_enrichment.py` do this automatically. For ad-hoc DB work, consider whether a custom `agent_runs` entry helps audit trail.
4. **Never claim success without verification** — always read back at least one row of what was written.
