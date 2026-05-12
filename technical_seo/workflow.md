# Technical SEO — Workflow Runbook

Runbook for the Technical SEO Agent. **Phase 1 is in progress** — `sitemap_validator.py` is built; the rest are planning stubs. When the user asks for a planned module, either:
1. Tell them the module doesn't exist and offer to implement it, or
2. Run a one-off equivalent manually and report results.

Commands assume repo root as working directory.

---

## Decision tree: which workflow runs

| User says / asks | Workflow section | Status |
|---|---|---|
| "run the site audit", "audit pages", "find on-page issues", "title/meta/h1 problems", "missing alt text", "redirect chains", "canonical issues" | [1. Site audit](#1-site-audit) | **Available** |
| "CWV", "Core Web Vitals", "page speed check" | [2. CWV monitor](#2-cwv-monitor) | **Available** |
| "validate sitemap", "discover pages", "find broken sitemap URLs" | [3. Sitemap / robots validation](#3-sitemap--robots-validation) | **Available** |
| "internal linking recommendations", "link equity flow", "orphan pages", "PageRank", "which pages need more inbound links" | [4. Internal link analysis](#4-internal-link-analysis) | **Available** |
| ~~5. Canonical + redirect check~~ | -- | Folded into Site audit |
| "show open technical issues", "what's broken right now" | [6. Query: open issues](#6-query-open-issues) | Available |
| "CWV trends over time" | [7. Query: CWV history](#7-query-cwv-history) | Available |

---

## 1. Site audit

**Module:** `site_auditor.py` — **Available now.**

Uses `common/connectors/crawler.py` (already built). Iterates pages from the `pages` table (seeded by `sitemap_validator`), fetches each, runs 12 detectors, and writes findings to `technical_issues`. Includes everything originally scoped to a separate `canonical_checker.py` (canonical mismatch, external canonical, redirect chains).

### Detectors

| Issue type | Severity | Trigger |
|---|---|---|
| `missing_title` | critical | no `<title>` |
| `short_title` | low | < 30 chars |
| `long_title` | low | > 60 chars |
| `missing_meta_description` | high | no meta description |
| `short_meta_description` | low | < 70 chars |
| `long_meta_description` | low | > 160 chars |
| `missing_h1` | high | no `<h1>` |
| `multiple_h1` | medium | > 1 `<h1>` |
| `missing_canonical` | medium | no canonical tag |
| `canonical_mismatch` | high | canonical URL ≠ final rendered URL |
| `canonical_external` | medium | canonical points to different origin |
| `missing_alt_text` | medium | any image lacks alt; details has count + examples |
| `thin_content` | medium | word_count below page-type-aware threshold |
| `missing_schema` | low | no JSON-LD AND no microdata |
| `noindex_meta` | high | noindex on home/pillar/service page |
| `redirect_chain_too_long` | medium | > 2 redirects in chain |

### Thin-content thresholds (word count by page_type)

| page_type | threshold |
|---|---|
| home | 150 |
| pillar | 800 |
| service | 300 |
| blog | 300 |
| resource | 200 |
| landing | 100 |
| glossary | 100 |

### Behavior

- Cadence-aware: skips pages whose `last_audited` is within `--cadence` days (default 7). Use `--all` to force.
- One issue per (url, issue_type). Re-running refreshes `details` (counts can change) rather than duplicating.
- Auto-resolves issues that are no longer triggered.
- Updates `pages.last_audited = now()` for every page audited.
- Logs to `agent_runs` with full issue counts in metadata.

### Command

```bash
# Default: all 3 domains, page_type IN (home, pillar, service), weekly cadence
python -m technical_seo.site_auditor

# One domain
python -m technical_seo.site_auditor --domain damcogroup.com

# Include blog + resource pages too
python -m technical_seo.site_auditor --page-types home,pillar,service,blog,resource

# Force re-audit ignoring cadence
python -m technical_seo.site_auditor --all

# Dry run — fetch + analyze but don't write
python -m technical_seo.site_auditor --dry-run
```

### Cost / time

- Free (no external paid APIs — only fetches the target pages).
- 4 parallel workers with the crawler's 1 req/sec/origin rate limit means same-domain fetches serialize at 1 req/sec.
- damcodigital.com (20 pages): ~20s validated
- achieva.ai (~15 default-scope pages): ~15s estimated
- damcogroup.com (~227 default-scope pages): ~4-5 min estimated

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

**Module:** `internal_link_analyzer.py` — **Available now.**

Self-contained: crawls all in-scope pages via the shared crawler connector, extracts internal `<a>` tags, populates the `internal_links` table, computes PageRank-style equity, and surfaces three classes of finding.

### What gets flagged

| Issue type | Severity | Trigger |
|---|---|---|
| `orphan_page` | medium | priority page (home/pillar/service) with 0 inbound internal links |
| `dead_end_page` | low | page with 0 outbound internal links |
| `underlinked_pillar` | high | pillar page with < 5 inbound internal links |
| `underlinked_service` | medium | service page with < 3 inbound internal links |

### Outputs

- **`internal_links` table** — UPSERT (UNIQUE on source+target+anchor). History preserved.
- **`technical_issues` table** — one issue per (url, type), auto-resolves when no longer triggered.
- **Narrative report** — `outputs/audits/internal_link_report_<date>[_<domain>].md` containing:
  - Graph stats (nodes, edges, avg outbound)
  - Top 10 pages by PageRank in scope
  - Orphan list (priority-type breakdown)
  - Dead-end list
  - Under-linked priority pages with **suggested source pages** (top high-PR pages in the same origin that don't currently link there)

### URL normalization

Internal-link rows normalize URLs aggressively to dedupe the graph:
- Lowercase scheme + host
- Strip trailing slash (except for root `/`)
- Strip URL fragments
- Default ports dropped

Path case preserved (some servers are case-sensitive on path).

### LLM-assisted recommendations

**Deferred to v2.** Rule-based source-page suggestions (top high-PR pages that don't link to the target yet) are already in the report. LLM-assisted natural anchor-text generation + topical-relevance scoring will be a future enhancement gated behind a `--with-recommendations` flag and `CLAUDE_MODEL_DEFAULT`.

### Command

```bash
# Default: all 3 domains, page_types = home/pillar/service
python -m technical_seo.internal_link_analyzer

# One domain
python -m technical_seo.internal_link_analyzer --domain damcogroup.com

# Wider scope — include blog + resource pages as graph nodes too
python -m technical_seo.internal_link_analyzer --page-types home,pillar,service,blog,resource

# Re-analyze the existing graph without re-crawling
python -m technical_seo.internal_link_analyzer --skip-crawl

# Dry run — analyze but don't write
python -m technical_seo.internal_link_analyzer --dry-run
```

### Cost / time

- Free (HTTP only).
- 4 parallel workers, 1 req/sec/origin rate limit.
- damcodigital.com (20 pages): ~20s validated. 557 edges in graph.
- achieva.ai (~15 default-scope pages): ~15s estimated.
- damcogroup.com (~227 default-scope pages): ~4-5 min estimated.

---

## 5. ~~Canonical + redirect check~~ -- folded into Site audit

The original plan had a separate `canonical_checker.py` module. It would have needed the exact same crawler.fetch() output as `site_auditor.py`, so splitting it out would double HTTP cost without separating any real concern.

Canonical mismatch, external canonical, and redirect-chain detection live in `site_auditor.py` (Section 1) as `canonical_mismatch`, `canonical_external`, and `redirect_chain_too_long` issue types. Run the site auditor to get these findings.

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
