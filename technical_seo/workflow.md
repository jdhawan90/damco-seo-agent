# Technical SEO — Workflow Runbook

Runbook for the Technical SEO Agent. **Phase 1 is in progress** — `sitemap_validator.py` is built; the rest are planning stubs. When the user asks for a planned module, either:
1. Tell them the module doesn't exist and offer to implement it, or
2. Run a one-off equivalent manually and report results.

Commands assume repo root as working directory.

---

## Decision tree: which workflow runs

| User says / asks | Workflow section | Status |
|---|---|---|
| "run the site audit", "crawl the sites", "find broken links" | [1. Site audit](#1-site-audit) | Planned |
| "CWV", "Core Web Vitals", "page speed check" | [2. CWV monitor](#2-cwv-monitor) | **Built** (needs API key) |
| "validate sitemap", "discover pages", "find broken sitemap URLs" | [3. Sitemap / robots validation](#3-sitemap--robots-validation) | **Available** |
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

**Module:** `cwv_monitor.py` — **Built. Blocked on PAGESPEED_API_KEY.**

### Prerequisite: obtain PageSpeed Insights API key (free, ~5 min)

PageSpeed Insights heavily rate-limits unauthenticated requests from server IPs (we observed 429 on the very first call). A free API key removes the limit and gives 25,000 queries/day.

To obtain:
1. Open https://console.cloud.google.com
2. Create or select a project
3. Enable the **"PageSpeed Insights API"** (search in API library)
4. Credentials → Create Credentials → API key
5. Optionally restrict to the PageSpeed API
6. Paste the key into `.env`: `PAGESPEED_API_KEY=<your_key>`

The connector auto-detects the key. No code change needed once the key is set.

### Behavior

- For each page in `pages` whose `page_type` matches the filter (default: `home`, `pillar`, `service`):
  - For each strategy (default: `mobile` + `desktop`):
    - If the latest snapshot for `(url, device)` is older than `--cadence` days (default 7), enqueue.
- Calls PageSpeed Insights in parallel (default 4 workers).
- Captures field-data-preferred CWV (LCP, INP, CLS) + Lighthouse performance score (0-100).
- Compares to previous snapshot for that `(url, device)` to detect ≥20% regressions in any metric.
- Writes `cwv_metrics` (one row per url/date/device).
- Opens `technical_issues`:
  - `cwv_below_threshold` (severity high) when score < threshold:
    - **Mobile threshold:** 60
    - **Desktop threshold:** 85
  - `cwv_regression` (severity medium) when any metric drops ≥20% vs the previous snapshot.
  - Both issue types include `details.device` so the same URL can have separate mobile/desktop issues.
- Auto-resolves issues that are no longer triggered.
- Logs to `agent_runs`.

### Command

```bash
# Default: all 3 domains, page_type IN (home, pillar, service), mobile + desktop, weekly cadence
python -m technical_seo.cwv_monitor

# One domain, all device strategies
python -m technical_seo.cwv_monitor --domain damcogroup.com

# Cover blog + resource pages too (much larger run)
python -m technical_seo.cwv_monitor --page-types home,pillar,service,blog,resource

# Force re-check ignoring cadence
python -m technical_seo.cwv_monitor --all

# Mobile only
python -m technical_seo.cwv_monitor --strategies mobile

# Dry run — call PageSpeed but don't write
python -m technical_seo.cwv_monitor --dry-run
```

### Cost / time

- Free with API key (25k queries/day quota).
- A typical Lighthouse audit takes 10–30s; 4 workers ≈ ~12s effective per call.
- Estimate for default scope (home + service pages across 3 domains, both devices):
  - damcogroup.com: ~226 service + 1 home = 227 pages × 2 = ~454 calls ≈ 23 min
  - damcodigital.com: 19 + 1 = 20 × 2 = 40 calls ≈ 2 min
  - achieva.ai: 14 × 2 = 28 calls ≈ 1.5 min
  - **Full default run: ~30 min**

---

## 3. Sitemap / robots validation

**Module:** `sitemap_validator.py` — **Available now.**

**Behavior:**
- Fetches the configured sitemap entry point for each of the 3 domains:
  - `damcogroup.com` → `https://www.damcogroup.com/sitemap.xml`
  - `damcodigital.com` → `https://damcodigital.com/sitemap_index.xml`
  - `achieva.ai` → `https://achieva.ai/sitemap.xml`
- Auto-handles sitemap indexes (recurses into sub-sitemaps).
- Validates every page URL with a HEAD request (GET fallback when HEAD is rejected). Follows redirects up to 5 hops.
- Auto-categorizes `page_type` by URL heuristic (home / blog / service / resource / glossary / landing). Leaves NULL for ambiguous pages and surfaces them in the report for human curation.
- Writes:
  - `pages` — UPSERT one row per discovered URL
  - `technical_issues` — opens issues for: `sitemap_url_broken` (4xx/5xx), `sitemap_url_redirect` (URL not canonical), `redirect_chain_too_long` (>2 hops), `sitemap_fetch_failed`
  - Auto-resolves issues whose URL is no longer broken in the current run
- Logs to `agent_runs` with metadata.

**Command:**
```bash
python -m technical_seo.sitemap_validator                    # all 3 domains
python -m technical_seo.sitemap_validator --domain damcogroup.com
python -m technical_seo.sitemap_validator --dry-run          # validate without DB writes
```

**Cadence:** weekly is fine; sitemaps don't change often. Sitemap validation is free (no API cost).

**Robots.txt check:** not yet implemented in this module. Will be a separate small module or a flag once we have a clearer set of forbidden paths to enforce.

**Typical run cost / time:**
- damcogroup.com (~1,200 URLs): 20–25 min sequential
- achieva.ai (~130 URLs): 2–3 min
- damcodigital.com (~40 URLs): 1.5 min
- Free (no API charges)

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
