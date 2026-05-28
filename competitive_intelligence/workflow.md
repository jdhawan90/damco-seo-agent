# Competitive Intelligence — Workflow Runbook

Runbook for the Competitive Intelligence Agent. `gap_analyzer.py` is **available now**; the other modules are still planned. The SERP-side schema (migration 004) is in place and populated with real data.

## Decision tree

| User says / asks | Section | Status |
|---|---|---|
| "what changed on competitor sites", "competitor page rewrites", "title changes", "new competitor pages", "removed pages" | [1. Competitor monitor](#1-competitor-monitor) | **Available** |
| "competitor backlinks", "where are they getting links", "outreach prospects", "common referring domains" | [2. Backlink analyzer](#2-backlink-analyzer) | **Built (needs subscription)** |
| "what are they publishing", "competitor blog tracker" | [3. Content monitor](#3-content-monitor) | Planned |
| "gap analysis", "what topics are we missing", "where competitors win and we don't", "displacement opportunities", "quick wins" | [4. Gap analyzer](#4-gap-analyzer) | **Available** |
| "add / remove / mute a competitor", "manage competitor roster" | [5. Competitor roster](#5-competitor-roster) | Available |
| "show competitor SERP positions for our keywords" | [6. Query: competitor SERPs](#6-query-competitor-serps) | Available |
| "who's competing in the AI offering", "share of voice", "top competitors per offering" | [7. Query: offering rollup](#7-query-offering-rollup) | Available |
| "what changed in the SERP", "weekly digest", "summary of changes", "what should I look at this week" | [11. Event digest](#11-event-digest) | **Available** |
| "what changed in the SERP" (raw query) | [8. Query: SERP event feed](#8-query-serp-event-feed) | Available |
| "who's outranking us right now", "displacement events" | [9. Query: damco displacement](#9-query-damco-displacement) | Available |
| "AI Overview citations", "GEO visibility per keyword" | [10. Query: AI Overview tracking](#10-query-ai-overview-tracking) | Available |

---

## 1. Competitor monitor

**Module:** `competitor_monitor.py` — **Available now.**

Crawls tracked competitor URLs (top-N ranking results per keyword) on a cadence and detects on-page changes that operations should know about between rank-tracker cycles.

### What's monitored

By default: every (competitor, url) pair where
- `competitor.threat_tier` is `primary` or `watch` AND
- the URL ranked top-10 for any of our active keywords in the latest snapshot.

Override via `--threat-tier`, `--offering`, `--top-n`.

State per URL is stored in **`competitor_pages`** (migration 007). Cadence is per-URL: a URL is re-crawled only if its `last_fetched_at` is older than `--cadence` days (default 7).

### Change events emitted

Each detected delta becomes one row in `competitor_changes`:

| `change_type` | Significance (0-1) | Trigger |
|---|---:|---|
| `new_page` | 0.40 | First time crawling this URL |
| `title_change` | 0.70 | `<title>` differs |
| `meta_change` | 0.50 | meta description differs |
| `structure_change` | 0.60 | H1 changed OR schema `@type` set changed |
| `content_update` | 0.40-0.80 | Only fires if no other change above; significance scales with word_count delta |
| `removed` | 0.50 | URL now returns 404 or 410 (true "gone" signals only — 403 bot-blocks and 5xx transient errors are filtered) |

### Status-code handling

- **200 + HTML** → normal diff path
- **404 / 410** → `removed` event (real signal)
- **403 / 401** → skipped (bot-blocked, not a content change)
- **5xx** → skipped (transient server error)
- **transport error** → skipped (our side / network)

This prevents bot-blocked competitor pages (clutch.co, bairesdev.com, etc.) from polluting the change stream with phantom "removed" events.

### Command

```bash
# Default: primary + watch tier competitors, top-10 URLs, all offerings, 7-day cadence
python -m competitive_intelligence.competitor_monitor

# Only primary threats (smaller scope)
python -m competitive_intelligence.competitor_monitor --threat-tier primary

# One offering
python -m competitive_intelligence.competitor_monitor --offering "AI"

# Wider net — also track top-20 placements
python -m competitive_intelligence.competitor_monitor --top-n 20

# Force re-crawl ignoring cadence
python -m competitive_intelligence.competitor_monitor --all

# Dry run
python -m competitive_intelligence.competitor_monitor --dry-run
```

### Cost / time

Free (HTTP only via shared crawler). Rate-limited 1 req/sec/origin by the crawler. 4 parallel workers.

Validated on primary-tier AI scope (52 URLs): ~40 seconds. First run produced 46 `new_page` events; immediate re-crawl produced 0 events (diff logic confirmed only firing on real changes).

---

## 2. Backlink analyzer

**Module:** `backlink_analyzer.py` — **Built. Blocked on DataForSEO Backlinks API subscription.**

### Prerequisite

Unlike the SERP API (pay-per-query), the Backlinks API requires a separate **monthly subscription**. Without it, calls return `status_code=40204` and the connector raises `DataForSEOAccessDenied`. The module degrades gracefully — generates a stub report explaining what's needed.

To activate:
1. Open https://app.dataforseo.com/backlinks-subscription
2. Pick a tier (typically $99-499/month depending on volume)
3. No code changes needed — the next run will work

### What it does

- Loads competitors by threat tier (default: `primary` only) or specific `--domain`.
- Calls DataForSEO `/v3/backlinks/backlinks/live` for each, with `--limit` results per competitor (default 500, max 1000).
- Upserts into `competitor_backlinks` (migration 008): source_url, source_domain, target_url, anchor_text, dofollow, domain_rank (0-100 authority), first_seen, last_seen.
- Cross-analyzes:
  - Top referring domains across all primary threats
  - **Outreach prospects** — referring domains linking to ≥2 primary threats (highest-leverage targets; they already publish about this space)
  - Anchor-text patterns competitors are building
- Per-URL cadence: re-pulls only if previous fetch is older than `--cadence` days (default 30).

### Outputs

- `outputs/reports/backlink_analysis_<date>.xlsx` — 4 sheets:
  - Per-Competitor (summary stats)
  - Top Referring Domains (all)
  - Outreach Prospects (≥2 competitors linked, not Damco)
  - Anchor Patterns
- `outputs/audits/backlink_analysis_<date>.md` — narrative report

### Command

```bash
# Default: primary threat tier, top 500 backlinks each, monthly cadence
python -m competitive_intelligence.backlink_analyzer

# Wider scope: primary + watch
python -m competitive_intelligence.backlink_analyzer --threat-tier primary,watch

# One specific competitor
python -m competitive_intelligence.backlink_analyzer --domain itransition.com

# Lower limit to control cost
python -m competitive_intelligence.backlink_analyzer --limit 100

# Force re-pull ignoring 30-day cadence
python -m competitive_intelligence.backlink_analyzer --all

# Generate reports from existing DB data only (no API calls)
python -m competitive_intelligence.backlink_analyzer --analyze-only

# Dry-run: calls API (real cost) but doesn't write to DB
python -m competitive_intelligence.backlink_analyzer --dry-run
```

### Cost model

Backlinks API is subscription-based, not pay-per-query. Once subscribed, calls are typically included in the monthly tier (within quota). Verify your current tier's call quota before running on all 14 primary threats × 500 backlinks each = 7,000 backlink records.

---

## 3. Content monitor

**Planned module:** `content_monitor.py`

**Behavior when built:** tracks new URLs indexed under competitor domains (via DataForSEO or Google site:queries), classifies by content type (blog vs. landing), extracts topic, flags anything competing for Damco's target keywords.

**Planned command:** `python -m competitive_intelligence.content_monitor`

---

## 4. Gap analyzer

**Module:** `gap_analyzer.py` — **Available now.**

Classifies every active keyword by competitive gap type using the populated `competitor_rankings` + `keyword_rankings` (DataForSEO + GSC) data.

### Gap taxonomy

| Type | Trigger | Action implication |
|---|---|---|
| `coverage_gap` | Damco not in top 100 AND ≥1 tracked competitor in top 10 | No competing page exists (or it's invisible). Content investment. |
| `displacement` | Damco at #11-30 AND competitor in top 10 | Page exists; needs targeted on-page work to push past specific competitors. Quick-win territory. |
| `cluster_win` | Same competitor wins top-10 placements for ≥3 keywords in the offering | They dominate a sub-niche; cluster strategy needed (multiple pages). |
| `low_priority` | Damco rank > 30 | Has content but far from page 1. Lower ROI unless GSC traffic is meaningful. |
| `none` | Damco in top 10 | Already defended. |

### Severity scoring (1-10)

GSC-traffic-weighted: gaps on keywords with real Google impressions/clicks score higher.

- Base: 3 (coverage), 4 (displacement)
- +0.4 per tracked competitor in top 10 (max +4)
- +2 if ≥100 GSC impressions in 14 days
- +3 if any GSC clicks in 14 days (real money at stake)
- +1 if any top-10 competitor is `threat_tier = primary`

### LLM narrative (optional)

`--with-narrative` invokes Claude (model = `CLAUDE_MODEL_DEFAULT`, default `sonnet-4-6`) to produce a per-offering executive summary + 5 prioritized recommendations with QUICK WIN vs INVESTMENT labels. Costs ~$0.02-0.05 per offering with Sonnet.

When ANTHROPIC_API_KEY is missing or credit is exhausted, falls back to a rule-based summary. The reports still generate; only the strategic narrative is downgraded.

### Outputs

- **`outputs/reports/gap_analysis_<date>.xlsx`** — single workbook covering whatever offerings ran:
  - Sheet 1 *Per-Keyword*: one row per active keyword with gap_type, severity, Damco position, GSC data, top 3 competitors
  - Sheet 2 *Cluster Wins*: every competitor winning ≥3 keywords in any offering
  - Sheet 3 *Summary*: per-offering totals
- **`outputs/audits/gap_analysis_<offering>_<date>.md`** — one markdown report per offering with narrative (LLM or rule-based), cluster wins table, full coverage/displacement lists

### Command

```bash
# All 15 offerings, rule-based summaries (default)
python -m competitive_intelligence.gap_analyzer

# One offering, LLM narrative
python -m competitive_intelligence.gap_analyzer --offering "AI" --with-narrative

# All offerings with LLM (cost: ~$0.30-0.75 with Sonnet)
python -m competitive_intelligence.gap_analyzer --with-narrative

# Generate reports without logging an agent_runs entry
python -m competitive_intelligence.gap_analyzer --dry-run
```

### Validation (2026-05-28)

- 15 offerings, 1,112 keywords analyzed in **1.2 seconds**
- 347 coverage gaps, 358 displacement gaps, 1,008 cluster wins flagged
- AI offering's top quick-win candidates surfaced correctly (ai consulting companies, ai software development variants, ai application development family)
- Top content-investment candidate `ai agent development` flagged correctly — page exists at `/ai-agent-development` but isn't ranking; GSC shows 8 clicks / 1,063 impressions waiting to be unlocked

---

## 5. Competitor roster

**Available now.** Most rows are auto-populated by `keyword_intelligence/rank_tracker.py` (any domain seen in a top 10 gets a stub row). Manual curation is for editing metadata or muting.

**Note:** `competitors.offering` was dropped in migration 004 — competitors span offerings via the `competitor_rankings` join. Don't reintroduce it.

List active competitors with full metadata:
```sql
SELECT competitor_domain, company_name, category, threat_tier,
       keyword_appearance_count, offering_appearance_count,
       first_seen_date, last_seen_date, is_tracked
FROM competitors
WHERE is_tracked = TRUE
ORDER BY threat_tier, keyword_appearance_count DESC;
```

Manually add a competitor (rare — most arrive via auto-stubbing):
```sql
INSERT INTO competitors (competitor_domain, company_name, category, status, is_tracked)
VALUES ('appinventiv.com', 'Appinventiv', 'direct', 'active', TRUE)
ON CONFLICT (competitor_domain) DO UPDATE
SET company_name = EXCLUDED.company_name,
    category     = EXCLUDED.category;
```

Mute a domain (e.g. wikipedia.org appearing in informational SERPs):
```sql
UPDATE competitors SET is_tracked = FALSE WHERE competitor_domain = 'wikipedia.org';
```

Update curation fields after manual review:
```sql
UPDATE competitors
SET category = 'aggregator',
    notes    = 'Listicle publisher; pursue inclusion rather than displacement',
    metadata = metadata || '{"target_listicles": ["top-10-ai-companies"]}'::jsonb
WHERE competitor_domain = 'leewayhertz.com';
```

---

## 6. Query: competitor SERPs

**Available now.** Per-keyword view — who's in the top 10 right now, with movement context.

```sql
SELECT cr.rank_position,
       c.competitor_domain,
       cr.url_found,
       cr.url_title,
       cr.page_type,
       cr.serp_features_owned,
       cr.is_new_entrant,
       cr.previous_position,
       cr.position_change
FROM competitor_rankings cr
JOIN competitors c ON c.id = cr.competitor_id
JOIN keywords k   ON k.id = cr.keyword_id
WHERE k.keyword = %s
  AND cr.date = (SELECT MAX(date) FROM competitor_rankings WHERE keyword_id = k.id)
  AND cr.rank_position BETWEEN 1 AND 10
ORDER BY cr.rank_position;
```

---

## 7. Query: offering rollup

**Available now.** `mv_offering_competition` is the per-offering share-of-voice rollup. Refresh runs at the end of each rank-tracker cycle.

Top 10 competitors for a single offering:
```sql
SELECT competitor_domain, company_name, category, threat_tier,
       keywords_in_top_10, total_keywords_in_offering,
       share_of_voice_pct, avg_top_10_position, best_position,
       last_seen_date
FROM mv_offering_competition
WHERE offering = %s
ORDER BY share_of_voice_pct DESC
LIMIT 10;
```

Cross-offering view of `primary` threat tier competitors:
```sql
SELECT offering, competitor_domain, share_of_voice_pct, keywords_in_top_10
FROM mv_offering_competition
WHERE threat_tier = 'primary'
ORDER BY offering, share_of_voice_pct DESC;
```

If the view looks stale (e.g. you just ran an ad-hoc snapshot), refresh it manually:
```sql
REFRESH MATERIALIZED VIEW CONCURRENTLY mv_offering_competition;
```

---

## 8. Query: SERP event feed

**Available now.** `competitor_serp_events` is the append-only stream of significant SERP changes. This is the primary trigger feed for digest generation.

High-severity events from the last 14 days:
```sql
SELECT e.event_date, e.event_type, e.severity,
       k.keyword, k.offering,
       c.competitor_domain,
       e.old_value, e.new_value, e.delta,
       e.metadata
FROM competitor_serp_events e
LEFT JOIN keywords    k ON k.id = e.keyword_id
LEFT JOIN competitors c ON c.id = e.competitor_id
WHERE e.event_date >= CURRENT_DATE - INTERVAL '14 days'
  AND e.severity IN ('critical', 'high')
ORDER BY e.severity, e.event_date DESC;
```

Event type breakdown for a single offering, last 30 days:
```sql
SELECT e.event_type,
       COUNT(*)                                      AS occurrences,
       COUNT(*) FILTER (WHERE e.severity = 'high')   AS high_count,
       COUNT(DISTINCT e.competitor_id)               AS distinct_competitors
FROM competitor_serp_events e
JOIN keywords k ON k.id = e.keyword_id
WHERE k.offering = %s
  AND e.event_date >= CURRENT_DATE - INTERVAL '30 days'
GROUP BY e.event_type
ORDER BY occurrences DESC;
```

Event severity filter for the digest agent (when `event_digest.py` is built, this is the read query):
```sql
SELECT * FROM competitor_serp_events
WHERE severity IN ('critical', 'high')
  AND created_at > (SELECT COALESCE(MAX(processed_at), NOW() - INTERVAL '14 days')
                    FROM agent_runs
                    WHERE agent_name = 'competitive_intelligence.event_digest'
                      AND status     = 'success')
ORDER BY event_date DESC;
```

---

## 9. Query: damco displacement

**Available now.** Identify cases where damco dropped while a competitor gained — the closest thing we have to a "they took our position" signal.

```sql
WITH damco_drops AS (
    SELECT keyword_id, event_date, old_value, new_value
    FROM competitor_serp_events
    WHERE event_type IN ('damco_position_change', 'damco_drops_top_n')
      AND event_date >= CURRENT_DATE - INTERVAL '14 days'
      AND (new_value->>'position')::INT > (old_value->>'position')::INT  -- drop = higher number
),
competitor_gains AS (
    SELECT e.keyword_id, e.event_date, e.competitor_id,
           c.competitor_domain, e.old_value, e.new_value
    FROM competitor_serp_events e
    JOIN competitors c ON c.id = e.competitor_id
    WHERE e.event_type IN ('position_gain', 'new_entrant')
      AND e.event_date >= CURRENT_DATE - INTERVAL '14 days'
)
SELECT k.keyword, k.offering,
       d.event_date,
       cg.competitor_domain,
       d.old_value->>'position'  AS damco_was,
       d.new_value->>'position'  AS damco_now,
       cg.new_value->>'position' AS competitor_now
FROM damco_drops d
JOIN competitor_gains cg
  ON cg.keyword_id = d.keyword_id
 AND cg.event_date = d.event_date
JOIN keywords k ON k.id = d.keyword_id
ORDER BY d.event_date DESC, k.offering;
```

---

## 10. Query: AI Overview tracking

**Available now.** `keyword_serp_snapshots` records AI Overview presence + cited domains per keyword, per snapshot.

Keywords where damco is/isn't cited in AI Overview:
```sql
SELECT k.keyword, k.offering,
       s.date,
       s.ai_overview_present,
       s.ai_overview_citations,
       (s.ai_overview_citations @> '[{"domain": "damcogroup.com"}]'::jsonb) AS damco_cited
FROM keyword_serp_snapshots s
JOIN keywords k ON k.id = s.keyword_id
WHERE s.date = (SELECT MAX(date) FROM keyword_serp_snapshots WHERE keyword_id = k.id)
  AND s.ai_overview_present = TRUE
ORDER BY damco_cited, k.offering, k.keyword;
```

Domains most frequently cited in AI Overview across all tracked keywords:
```sql
SELECT citation->>'domain' AS domain,
       COUNT(*)             AS citation_count,
       COUNT(DISTINCT s.keyword_id) AS distinct_keywords
FROM keyword_serp_snapshots s,
     LATERAL jsonb_array_elements(s.ai_overview_citations) citation
WHERE s.date >= CURRENT_DATE - INTERVAL '30 days'
GROUP BY citation->>'domain'
ORDER BY citation_count DESC
LIMIT 20;
```

---

## 11. Event digest

**Module:** `event_digest.py` — **Available now.**

Reads `competitor_serp_events` and produces a markdown digest of changes that happened in the SERPs since the last digest run. This is the alert layer — the question it answers is *"what should I look at this week?"*.

### What it shows

By default: events with severity `critical`, `high`, or `medium`. Add `--all-severity` to include `low`/`info` (mostly noise).

Events are grouped into report sections:

1. **🚨 Damco-side movements** — `damco_drops_top_n`, `damco_enters_top_n`, `damco_position_change` (our own SERP positions changing)
2. **⚠️ Competitor entries & exits** — `new_entrant`, `drop_out` (top-10 churn)
3. **📊 Position movements** — `position_gain`, `position_drop` (competitors moving ≥3 positions)
4. **Threat-tier changes & first sightings** — `threat_tier_changed` (especially promotions to `primary`), `first_seen_anywhere`
5. **SERP feature changes** — `serp_feature_appeared`/`_disappeared` (AI Overview, featured snippet, etc.)

### "Since when" resolution

The lower bound for events is computed in this order:

1. `--since YYYY-MM-DD` flag (explicit override)
2. Otherwise: the latest successful `agent_runs` row for this agent's name — events from that date forward
3. Otherwise (first run ever): default to last 14 days

This means a recurring schedule produces a non-overlapping digest each cycle.

### LLM editorial summary (optional)

`--with-narrative` adds a 2-3 sentence "what happened" paragraph + 3 tagged action bullets (URGENT / THIS WEEK / BACKLOG) at the top of the digest. Falls back to rule-based summary when ANTHROPIC_API_KEY is missing or credit exhausted.

### Outputs

`outputs/audits/serp_event_digest_<since>_to_<today>[_<offering>].md`

### Command

```bash
# Default: events since last successful digest, OR last 14 days if first run
python -m competitive_intelligence.event_digest

# One offering
python -m competitive_intelligence.event_digest --offering "AI"

# Custom window
python -m competitive_intelligence.event_digest --since 2026-05-01

# Include LLM editorial summary
python -m competitive_intelligence.event_digest --with-narrative

# Include low + info severity (very noisy)
python -m competitive_intelligence.event_digest --all-severity

# Dry run — generate report, skip agent_runs DB write
python -m competitive_intelligence.event_digest --dry-run
```

### Validation (2026-05-28)

- 1,207 events analyzed across 2026-05-15 → 2026-05-28 in 0.15s
- Surfaced 14 new primary-threat promotions (`itransition.com` with 90 kw across 10 offerings; `toptal.com` 21 kw / 8 offerings; etc.) — the actionable signal
- Collapsed 1,193 noisy peripheral→watch baseline tier-changes into a single summary line so they don't bury the real signal

---

## What to always do

1. Present findings per competitor per offering — not a single giant list.
2. Route insights to the right agent: new keywords → `keyword_intelligence/`, new platforms → `offpage_links/`, new topics → `content_operations/`.
3. Log significant gap analysis findings to `memory/monitoring/`.
