# Technical SEO — Workflow Runbook

Runbook for the Technical SEO Agent. All four modules are built. Commands assume repo root as working directory.

---

## Prerequisites (do this first)

**1. Apply migrations.** Nothing in this folder runs without a tenant row — every module calls `common.tenant.profile()` and `TenantNotConfigured` is raised loudly rather than defaulting to somebody's domain.

```bash
python sql/migrate.py          # idempotent; safe to re-run
```

Migration 012 creates and seeds `tenants`, `tenant_domains`, `tenant_offerings`, `tenant_vocabularies`, `tenant_policies`. Set `TENANT_SLUG` in `.env` only if the database holds more than one tenant.

**2. Check what has actually run.** Don't infer state from the table below — read the registry:

```bash
python -m common.agents --folder technical_seo    # last run + status + blockers
python -m common.agents --validate                # catalogue vs filesystem
```

### What comes from the tenant profile, not from code

No module here contains a domain, a threshold, or a page-type list. Change these in `tenant_policies` / `tenant_domains`, never in Python:

| What | Where it lives | Used by |
|---|---|---|
| Domains + their sitemap entry points | `tenant_domains` (`domain`, `sitemap_url`, `role`) | `sitemap_validator` (all), `--domain` filters (all) |
| `audit_page_types` | `tenant_policies` — currently `["home","pillar","service"]` | default `--page-types` for `site_auditor`, `cwv_monitor`, `internal_link_analyzer`; also the must-index set for `noindex_meta` |
| `cwv_thresholds` | `tenant_policies` — currently `{"mobile":60,"desktop":85}` | `cwv_monitor` |
| `thin_content_thresholds` | `tenant_policies` — word-count floors by `page_type` | `site_auditor` |
| `inbound_link_thresholds` | `tenant_policies` — currently `{"pillar":5,"service":3}` | `internal_link_analyzer` |
| `url_path_map`, `service_keywords` | `tenant_vocabularies` | `sitemap_validator` page-type rules |
| Crawler identity | `tenants.crawler_bot_name` + `crawler_contact_url` | every fetch made by the crawler and the sitemap walker |

The crawler and the sitemap walker send the profile's user agent (currently `DamcoSEOBot/1.0 (+https://www.damcogroup.com/; SEO ops monitoring)`), falling back to an unbranded `SEOBot/1.0` if the profile can't be reached. That string is what `robots.txt` evaluation matches against, so don't change it casually.

---

## Decision tree: which workflow runs

| User says / asks | Workflow section | Status |
|---|---|---|
| "run the site audit", "audit pages", "find on-page issues", "title/meta/h1 problems", "missing alt text", "redirect chains", "canonical issues" | [1. Site audit](#1-site-audit) | **Available** |
| "CWV", "Core Web Vitals", "page speed check" | [2. CWV monitor](#2-cwv-monitor) | **Available** |
| "validate sitemap", "discover pages", "find broken sitemap URLs" | [3. Sitemap / robots validation](#3-sitemap--robots-validation) | **Available** |
| "internal linking recommendations", "link equity flow", "orphan pages", "PageRank", "which pages need more inbound links" | [4. Internal link analysis](#4-internal-link-analysis) | **Blocked on a one-line bug** — read the section first |
| ~~5. Canonical + redirect check~~ | -- | Folded into Site audit |
| "show open technical issues", "what's broken right now" | [6. Query: open issues](#6-query-open-issues) | Available |
| "CWV trends over time" | [7. Query: CWV history](#7-query-cwv-history) | Available |

---

## 1. Site audit

**Module:** `site_auditor.py` — **Available now.**

Uses `common/connectors/crawler.py` (already built). Iterates pages from the `pages` table (seeded by `sitemap_validator`), fetches each, runs 17 per-page detectors plus a 2-check cross-page post-pass, and writes findings to `technical_issues`. Includes everything originally scoped to a separate `canonical_checker.py` (canonical mismatch, external canonical, redirect chains).

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
| `missing_alt_text` | medium | any real image (excluding `data:` placeholders) lacks alt; details has count + examples |
| `thin_content` | medium | word_count below page-type-aware threshold |
| `missing_schema` | low | no JSON-LD AND no microdata |
| `invalid_schema` | medium | known JSON-LD `@type` is missing schema.org required fields (e.g. `Organization` without `url`, `FAQPage` without `mainEntity`) |
| `noindex_meta` | high | noindex on a page whose `page_type` is in `audit_page_types` |
| `redirect_chain_too_long` | medium | > 2 redirects in chain |
| `duplicate_title` (post-pass) | high | another page in the same origin has the same title (case-insensitive). Details list up to 10 other URLs. |
| `duplicate_meta_description` (post-pass) | medium | another page in the same origin has the same meta description |

### Thin-content thresholds (word count by page_type)

From `tenant_policies.thin_content_thresholds` — not a module constant. A `page_type` absent from the map is never flagged thin. Current values:

| page_type | threshold |
|---|---|
| home | 150 |
| pillar | 800 |
| service | 300 |
| blog | 300 |
| resource | 200 |
| landing | 100 |
| glossary | 100 |

Read the live values rather than trusting this table:

```sql
SELECT value FROM tenant_policies WHERE key = 'thin_content_thresholds';
```

### Behavior

- Cadence-aware: skips pages whose `last_audited` is within `--cadence` days (default 7). Use `--all` to force.
- One issue per (url, issue_type). Re-running refreshes `details` (counts can change) rather than duplicating.
- Auto-resolves issues that are no longer triggered.
- Persists audit-time metadata to `pages` for every successful audit: `title`, `meta_description`, `canonical_url`, `lang`, `word_count`, `last_audited`. This data powers the cross-page duplicate detectors and is available for ad-hoc SQL queries.
- Cross-page duplicate post-pass: after every audited page's metadata is committed, the auditor compares newly-audited titles + meta descriptions against ALL pages in the same origin (case-insensitive) and emits `duplicate_title` / `duplicate_meta_description` issues. Auto-resolves when the duplicate clears.
- Logs to `agent_runs` with full issue counts in metadata.

### Root-cause clustering (console only, advisory)

After the issue counts, the run prints a **"Likely shared causes"** block. Issues are grouped by (issue type × first URL path segment) and a group is reported only when it has **≥4 issues** and **≥60%** of them sit under the same prefix. 43 `missing_meta_description` all under `/blogs/` is one broken template and one fix, not 43 tickets.

The counting is pure Python. The one generated thing is a single line per cluster naming the likely shared cause and the single fix that would clear the group — one batched `cheap`-tier call per run over the aggregates, never per page. If the model is unavailable the counts still print.

**This never writes to `technical_issues`, and it must not.** Every module in this folder opens, updates and auto-resolves rows in that table, and the state machine depends on the same input producing the same issue set every run. Put a generated judgement in the detector path and issues flap open and closed each cycle, which corrupts `date_resolved` history permanently. The clustering output is a reading of the run, not part of it.

### Command

```bash
# Default: every domain in the tenant profile, page types from
# `audit_page_types`, weekly cadence
python -m technical_seo.site_auditor

# One domain
python -m technical_seo.site_auditor --domain damcogroup.com

# Include blog + resource pages too
python -m technical_seo.site_auditor --page-types home,pillar,service,blog,resource

# Force re-audit ignoring cadence
python -m technical_seo.site_auditor --all

# Dry run — fetch + analyze but don't write
python -m technical_seo.site_auditor --dry-run

# More/fewer parallel crawler workers (default 4)
python -m technical_seo.site_auditor --workers 8
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

- For each page in `pages` whose `page_type` matches the filter (default: `audit_page_types` from the tenant policy):
  - For each strategy (default: `mobile` + `desktop`):
    - If the latest snapshot for `(url, device)` is older than `--cadence` days (default 7), enqueue.
- Calls PageSpeed Insights in parallel (default 4 workers).
- Captures field-data-preferred CWV (LCP, INP, CLS) + Lighthouse performance score (0-100).
- Compares to previous snapshot for that `(url, device)` to detect ≥20% regressions in any metric.
- Writes `cwv_metrics` (one row per url/date/device).
- Opens `technical_issues`:
  - `cwv_below_threshold` (severity high) when score < the pass mark for that device. Pass marks come from `tenant_policies.cwv_thresholds`, currently **mobile 60 / desktop 85**. These are this client's own baseline, not Google's 90/50 bands — they were set when damcodigital.com scored 16 on mobile. To retune, update the policy row; there is no constant in the module.
  - `cwv_regression` (severity medium) when any metric drops ≥20% vs the previous snapshot.
  - Both issue types include `details.device` so the same URL can have separate mobile/desktop issues.
- Auto-resolves issues that are no longer triggered.
- Logs to `agent_runs`.

### Command

```bash
# Default: every domain in the tenant profile, page types from
# `audit_page_types`, mobile + desktop, weekly cadence
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
- Estimate for default scope (home + service pages across the three configured domains, both devices):
  - damcogroup.com: ~226 service + 1 home = 227 pages × 2 = ~454 calls ≈ 23 min
  - damcodigital.com: 19 + 1 = 20 × 2 = 40 calls ≈ 2 min
  - achieva.ai: 14 × 2 = 28 calls ≈ 1.5 min
  - **Full default run: ~30 min**

---

## 3. Sitemap / robots validation

**Module:** `sitemap_validator.py` — **Available now.**

**Behavior:**
- Reads its target list from `tenant_domains` — there is no `DOMAINS` constant in the module any more. Each enabled row supplies a `domain` and its `sitemap_url`:

  ```sql
  SELECT domain, role, sitemap_url FROM tenant_domains WHERE enabled ORDER BY role, domain;
  ```

  Currently: `damcogroup.com` → `https://www.damcogroup.com/sitemap.xml`, `damcodigital.com` → `https://damcodigital.com/sitemap_index.xml`, `achieva.ai` → `https://achieva.ai/sitemap.xml`.
- **A row with a NULL `sitemap_url` is not skipped** — `common.sitemap.discover_sitemap_urls()` probes `/sitemap.xml`, `/sitemap_index.xml` and the `robots.txt` declarations, and the first hit is used. Only a domain where discovery finds nothing is skipped (logged as an error). Adding a new property is therefore an INSERT into `tenant_domains`, nothing more.
- Auto-handles sitemap indexes (recurses into sub-sitemaps).
- Validates every page URL with a HEAD request (GET fallback when HEAD is rejected). Follows redirects up to 5 hops.
- Categorizes `page_type` in two passes — see below.
- Writes:
  - `pages` — UPSERT one row per discovered URL
  - `technical_issues` — opens issues for: `sitemap_url_broken` (4xx/5xx), `sitemap_url_redirect` (URL not canonical), `redirect_chain_too_long` (>2 hops), `sitemap_fetch_failed`
  - Auto-resolves issues whose URL is no longer broken in the current run
- Logs to `agent_runs` with metadata.

### Page-type classification: rules, then a fallback

**Pass 1 — rules (deterministic).** Path segments are matched against `tenant_vocabularies` kind `url_path_map`, longest segment first, so `/industries_success/` (resource) beats `/industries/` (service). A row with a NULL label means "recognised but deliberately out of scope" — the WordPress taxonomy archives — and returns no type rather than falling through. Anything still unplaced is checked against `service_keywords` (substring anywhere in the path). Still unplaced → NULL.

**Pass 2 — model fallback.** The NULL set from pass 1 goes to one batched `cheap`-tier call per domain (cap 150 URLs) that returns `{url: page_type}` for the seven valid types. Answers outside that list, or for URLs not in the batch, are dropped — those stay NULL for human review rather than being guessed into the wrong bucket. Any failure returns nothing and the run is otherwise unchanged.

This is the portability fix as much as an accuracy one: previously a new client needed someone to hand-edit the service-keyword list before the classifier produced anything useful. Now the rules handle what they can and the fallback covers the rest.

The console summary prints how many URLs the fallback placed, separately from the rule-based counts. Use `--no-llm` for a fully deterministic run.

Results are persisted via `INSERT … ON CONFLICT (url) DO UPDATE SET page_type = COALESCE(pages.page_type, EXCLUDED.page_type)` — **first classification wins and is never overwritten**, so a hand-corrected `page_type` survives every later run.

**Command:**
```bash
python -m technical_seo.sitemap_validator                    # every enabled tenant domain
python -m technical_seo.sitemap_validator --domain damcogroup.com
python -m technical_seo.sitemap_validator --dry-run          # validate without DB writes
python -m technical_seo.sitemap_validator --no-llm           # rules only; leave the rest NULL
```

**Cadence:** weekly is fine; sitemaps don't change often. HTTP validation is free. The only cost is the page-type fallback — one cheap-tier call per domain per run, and only when the rules left something unplaced.

**Robots.txt check:** not yet implemented in this module. Will be a separate small module or a flag once we have a clearer set of forbidden paths to enforce.

**Typical run cost / time:**
- damcogroup.com (~1,200 URLs): 20–25 min sequential
- achieva.ai (~130 URLs): 2–3 min
- damcodigital.com (~40 URLs): 1.5 min
- No paid API charges; page-type fallback is one cheap-tier call per domain

---

## 4. Internal link analysis

**Module:** `internal_link_analyzer.py` — **Built. Currently crashes at runtime; see below before scheduling a run.**

> **Known defect (found 2026-07-27).** `find_underlinked()` references an undefined name `thresholds` where it should call `inbound_thresholds()`, left behind when the module moved its floors to the tenant policy. It is on the unconditional path, so **every** run raises `NameError: name 'thresholds' is not defined` — including `--skip-crawl` and `--dry-run`. Reproduce in one line:
> ```bash
> python -c "import sys;sys.path.insert(0,'.');from technical_seo.internal_link_analyzer import find_underlinked;find_underlinked([],[])"
> ```
> One-word fix in `technical_seo/internal_link_analyzer.py`. Delete this note once it lands.

Self-contained: crawls all in-scope pages via the shared crawler connector, extracts internal `<a>` tags, populates the `internal_links` table, computes PageRank-style equity, and surfaces three classes of finding.

There is no cadence filter in this module — every run re-crawls the full scope. It has no `--all` flag; use `--skip-crawl` to re-analyze the stored graph instead.

### What gets flagged

Inbound floors come from `tenant_policies.inbound_link_thresholds` (currently `{"pillar": 5, "service": 3}`). "Priority page" means a `page_type` in `audit_page_types`.

| Issue type | Severity | Trigger |
|---|---|---|
| `orphan_page` | medium | priority page with 0 inbound internal links |
| `dead_end_page` | low | page with 0 outbound internal links |
| `underlinked_pillar` | high | pillar page with inbound links below its policy floor |
| `underlinked_service` | medium | service page with inbound links below its policy floor |

The floors were calibrated on a 20-page property. On a large site, global nav alone clears them and the detector goes permanently silent — raise them in the policy row when scope grows.

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

**Still deferred — this module calls no model.** Rule-based source-page suggestions (top high-PR pages that don't link to the target yet) are already in the report. Anchor text is collected into `internal_links` and never read back; a batch pass over that column is the highest-value AI addition available in this folder, but it does not exist yet. There is no `--with-recommendations` flag. Don't tell the user this module generates anchor text.

### Command

```bash
# Default: every domain in the tenant profile, page types from `audit_page_types`
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
2. Suggest what to fix first — by severity, then by pillar-page importance. When `site_auditor` printed a "Likely shared causes" block, lead with that: one template fix usually outranks ten individual tickets.
3. Log non-trivial ad-hoc actions to `agent_runs` for the audit trail.
4. Never mark a technical issue as resolved without verifying the fix at the URL.
5. Confirm the run landed: `python -m common.agents --folder technical_seo`.

## Determinism contract

All four detector paths are 100% deterministic. Two modules call a model, and neither does so inside a detector:

| Module | Model use | Where the output goes |
|---|---|---|
| `sitemap_validator` | page-type fallback for URLs the rules can't place | `pages.page_type`, written once per URL and never overwritten. Disable with `--no-llm`. |
| `site_auditor` | one line per issue cluster naming the likely shared cause | console only, advisory |
| `cwv_monitor` | none | — |
| `internal_link_analyzer` | none | — |

One caveat worth knowing: `page_type` selects audit scope and picks the thin-content floor, so a fallback classification does influence what gets audited. The COALESCE write is what keeps that stable — a URL is classified once, and re-running never re-decides it.

Anything added later must follow the same shape: a separate advisory surface, or a column cached once per new entity. Never inline into a detector — `technical_issues` needs identical input to yield an identical issue set, or issues flap between runs and `date_resolved` history is corrupted.
