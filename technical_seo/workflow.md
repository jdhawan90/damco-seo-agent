# Technical SEO — Workflow Runbook

Runbook for the Technical SEO Agent. **The agent is not yet implemented** — most sections below are planning stubs. When the user asks you to perform an action, either:
1. Tell them the module doesn't exist and offer to implement it, or
2. Run a one-off equivalent manually and report results.

Commands assume repo root as working directory.

---

## Decision tree: which workflow runs

| User says / asks | Workflow section | Status |
|---|---|---|
| "run the site audit", "crawl the sites", "find broken links" | [1. Site audit](#1-site-audit) | Planned |
| "CWV", "Core Web Vitals", "page speed check" | [2. CWV monitor](#2-cwv-monitor) | Planned |
| "validate sitemap", "robots.txt check" | [3. Sitemap / robots validation](#3-sitemap--robots-validation) | Planned |
| "internal linking recommendations", "link equity flow" | [4. Internal link analysis](#4-internal-link-analysis) | Planned |
| "redirect chains", "canonical issues" | [5. Canonical + redirect check](#5-canonical--redirect-check) | Planned |
| "show open technical issues", "what's broken right now" | [6. Query: open issues](#6-query-open-issues) | Available |
| "CWV trends over time" | [7. Query: CWV history](#7-query-cwv-history) | Available |

---

## 1. Site audit

**Planned module:** `site_auditor.py`

**Behavior when built:**
- Crawls all three domains (`damcogroup.com`, `achieva.ai`, `damcodigital.com`) — max 500 pages per domain unless overridden.
- Detects: broken links (4xx/5xx), missing meta titles/descriptions, duplicate content, missing schema, title length violations, H1 hierarchy issues.
- Writes each finding to `technical_issues` with severity (`critical` / `high` / `medium` / `low` / `info`).
- Logs run to `agent_runs` with records_processed = issue count.

**Planned command:**
```bash
python -m technical_seo.site_auditor [--domain damcogroup.com] [--max-pages 500]
```

**Implementation checklist** (when building):
- [ ] Create `common/connectors/crawler.py` (HTTP + BeautifulSoup + robots.txt respect)
- [ ] Implement crawl queue with rate limiting (1 req/sec/domain default)
- [ ] Define issue detection rules (start with the 6 most common)
- [ ] Upsert into `technical_issues` with `ON CONFLICT (url, issue_type)` — mark old as resolved if no longer present
- [ ] Add `--dry-run` flag

**Workaround until built:** Use Screaming Frog or Sitebulb manually; manually log critical findings to `technical_issues`.

---

## 2. CWV monitor

**Planned module:** `cwv_monitor.py`

**Behavior when built:**
- Calls PageSpeed Insights for each page in `pages` table (or a hardcoded list of pillar pages initially).
- Captures field + lab data for LCP, INP, CLS, and overall performance score.
- Stores a row per (url, date, device) in `cwv_metrics`.
- Alerts when a URL regresses more than 20% on any metric.

**The connector is already built.** This module can be written cleanly on top of `common.connectors.pagespeed`:

```python
from common.connectors.pagespeed import get_cwv_metrics
result = get_cwv_metrics(url, strategy="mobile")
# result = {"url", "strategy", "performance_score", "lcp_ms", "inp_ms", "cls", "source", "raw"}
```

**Planned command:**
```bash
python -m technical_seo.cwv_monitor [--strategy mobile] [--pages-table]
```

**Workaround until built (one-off):**
```python
import sys; sys.path.insert(0, '.')
from common.connectors.pagespeed import get_cwv_metrics
from common.database import connection

for url in ["https://www.damcogroup.com/", "https://www.damcogroup.com/ai-agent-development", ...]:
    m = get_cwv_metrics(url, strategy="mobile")
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO cwv_metrics (url, lcp_ms, inp_ms, cls_score, performance_score, device)
                           VALUES (%s, %s, %s, %s, %s, 'mobile')
                           ON CONFLICT (url, date, device) DO UPDATE SET
                             lcp_ms = EXCLUDED.lcp_ms, inp_ms = EXCLUDED.inp_ms,
                             cls_score = EXCLUDED.cls_score,
                             performance_score = EXCLUDED.performance_score""",
                        (m["url"], m["lcp_ms"], m["inp_ms"], m["cls"], m["performance_score"]))
```

---

## 3. Sitemap / robots validation

**Planned module:** `sitemap_validator.py`

**Behavior when built:**
- Fetches `/sitemap.xml` and `/robots.txt` for each domain.
- Parses sitemap; validates every URL returns 200 and matches canonical version.
- Checks robots.txt for accidental disallows on important paths.
- Writes findings to `technical_issues` with `issue_type = 'sitemap_gap'` or `'robots_blocking'`.

**Planned command:**
```bash
python -m technical_seo.sitemap_validator [--domain damcogroup.com]
```

---

## 4. Internal link analysis

**Planned module:** `internal_link_analyzer.py`

**Behavior when built:**
- Uses `internal_links` table populated by the site crawler.
- Computes PageRank-style link equity flow across the site graph.
- Identifies pillar pages that could use more incoming internal links from blog posts.
- Produces a ranked list of (source_url, target_url, anchor) recommendations.

**LLM-assisted:** Yes — uses `CLAUDE_MODEL_DEFAULT` to generate natural anchor text and evaluate topical relevance of source pages.

**Planned command:**
```bash
python -m technical_seo.internal_link_analyzer --target-pillar "AI Agent Development"
```

---

## 5. Canonical + redirect check

**Planned module:** `canonical_checker.py`

**Behavior when built:**
- Follows every URL in `pages` and `sitemap` through their redirect chain.
- Flags chains longer than 2 hops, loops, and 302s where a 301 should exist.
- Compares canonical tags against actual rendered URL; flags mismatches.

**Planned command:**
```bash
python -m technical_seo.canonical_checker
```

---

## 6. Query: open issues

**Available now** — read directly from `technical_issues`.

```sql
SELECT url, issue_type, severity, date_found, details
FROM technical_issues
WHERE date_resolved IS NULL
ORDER BY
    CASE severity
        WHEN 'critical' THEN 0
        WHEN 'high' THEN 1
        WHEN 'medium' THEN 2
        WHEN 'low' THEN 3
        ELSE 4
    END,
    date_found DESC;
```

Present as a table grouped by severity. If the table is empty, tell the user no audits have been run yet.

---

## 7. Query: CWV history

**Available now** — read from `cwv_metrics`.

```sql
SELECT url, date, device, lcp_ms, inp_ms, cls_score, performance_score
FROM cwv_metrics
WHERE url = %s
ORDER BY date DESC
LIMIT 30;
```

Highlight any 20%+ regressions between consecutive dates.

---

## Ad-hoc data import

If the user provides URL lists, crawl reports, or CWV exports to load into the DB:
- Write the import inline, run it, verify counts, delete the script.
- **Never commit the loader** — same rule as keyword_intelligence.

---

## What to always do after any workflow

1. Show results, don't just say "done".
2. Suggest what to fix first — by severity, then by pillar-page importance.
3. Log non-trivial ad-hoc actions to `agent_runs` for the audit trail.
4. Never mark a technical issue as resolved without verifying the fix at the URL.
