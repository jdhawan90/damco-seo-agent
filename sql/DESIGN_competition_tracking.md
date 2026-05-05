# Competition Tracking — Schema Design Proposal

**Status:** Draft (pre-migration). Review and approve before SQL is written.
**Author:** SEO agent system
**Date:** 2026-04-14

---

## Goals

1. **Keyword-level competition** — for every tracked keyword, capture exactly who appears in the top N over time, with enough metadata to answer "who is outranking us, and what changed?"
2. **Offering-level competition** — roll up keyword-level data to answer "for the AI offering, who are our top 10 competitors right now and how is share-of-voice trending?"
3. **Active competitor registry** — one row per competitor domain, enriched with metadata (category, threat tier, DA, country) so a competitive analysis agent has a single source of truth.
4. **Change events** — append-only stream of significant SERP changes (new entrants, position swings, damco displacements) that downstream agents can subscribe to.
5. **Update discipline** — clear rules for when each table is written to, and never silently lose history.

---

## Existing Schema (What We Have)

Migration `001_initial_schema.sql` already created:

| Table | What it stores | Gaps for our use case |
|-------|---------------|----------------------|
| `competitors` | id, competitor_domain, offering, date_last_crawled, status, notes | Missing: company name, category, threat tier, DA, country, first/last seen, appearance count |
| `competitor_rankings` | id, competitor_id, keyword_id, date, rank_position, url_found | Missing: URL title, page type (service/listicle/docs), SERP feature ownership |
| `competitor_changes` | id, competitor_id, url, change_type, date_detected, diff_summary, significance_score | Currently scoped to *content* changes (new_page, content_update, etc.) — not the *ranking* events we need. Needs separate table or extended enum |

We will **extend** these rather than create parallel structures.

---

## Proposed Changes

### 1. Extend `competitors` (master registry)

Add columns:

| Column | Type | Purpose |
|--------|------|---------|
| `company_name` | TEXT | Human-readable name (e.g. "McKinsey & Company") |
| `category` | TEXT CHECK (direct, adjacent, aggregator, informational, big_tech, internal) | Buckets the threat type |
| `threat_tier` | TEXT CHECK (primary, watch, peripheral, ignore) | Computed: primary if appears in top 10 for >= 2 keywords AND category in (direct, aggregator) |
| `domain_authority` | INTEGER | From DataForSEO backlink data |
| `country` | TEXT | ISO 2-char code |
| `first_seen_date` | DATE | First time domain entered any tracked top N |
| `last_seen_date` | DATE | Most recent snapshot where domain appeared in top N |
| `keyword_appearance_count` | INTEGER DEFAULT 0 | Across all keywords currently in top N (denormalized for query speed) |
| `offering_appearance_count` | INTEGER DEFAULT 0 | How many offerings they overlap on |
| `is_tracked` | BOOLEAN DEFAULT TRUE | Manual curation flag — set FALSE to mute a domain (e.g. wikipedia.org) |
| `metadata` | JSONB DEFAULT '{}' | Flexible bucket for tags, notes, alternate names |

Drop the `offering` column (a competitor can compete on many offerings — represented via `competitor_rankings` join).

### 2. Extend `competitor_rankings` (per-keyword SERP snapshots)

Add columns:

| Column | Type | Purpose |
|--------|------|---------|
| `url_title` | TEXT | Page title from SERP — context for analysis |
| `page_type` | TEXT CHECK (service, listicle, blog, docs, product, homepage, unknown) | Lets us filter "real" competitors vs aggregators vs informational pages |
| `serp_features_owned` | JSONB DEFAULT '[]' | Array of features this URL owns: featured_snippet, ai_overview_cited, paa_present, image_pack, etc. |
| `is_new_entrant` | BOOLEAN DEFAULT FALSE | TRUE if this domain wasn't in the previous snapshot for this keyword |
| `position_change` | INTEGER | Delta from previous snapshot (positive = improvement, negative = decline, NULL = first snapshot) |
| `previous_position` | INTEGER | Position in previous snapshot (NULL if not present) |

The existing `UNIQUE (competitor_id, keyword_id, date)` stays — one row per (domain, keyword, snapshot date).

### 3. New table: `keyword_serp_snapshots` (one row per keyword per snapshot)

Captures keyword-level SERP metadata that doesn't belong on individual ranking rows.

```
id                  BIGSERIAL PK
keyword_id          BIGINT FK → keywords(id)
date                DATE
location_code       INTEGER     -- DataForSEO location code
device              TEXT CHECK (mobile, desktop)
serp_features       JSONB       -- ['ai_overview', 'featured_snippet', 'paa', 'image_pack', 'local_pack']
ai_overview_present BOOLEAN
ai_overview_citations JSONB     -- domains cited in AI Overview, if any
total_results_seen  INTEGER
damco_position      INTEGER     -- denormalized convenience copy from keyword_rankings
damco_url           TEXT
top_10_domains      JSONB       -- denormalized array for quick reads ['mckinsey.com', 'bcg.com', ...]
created_at          TIMESTAMPTZ
UNIQUE (keyword_id, date, device)
```

Why a separate table? `competitor_rankings` is one-row-per-competitor-per-snapshot. We also need one-row-per-snapshot context (SERP features, AI Overview content, our own position). Splitting these keeps writes clean.

### 4. New table: `competitor_serp_events` (change-log stream)

Append-only event log. Distinct from `competitor_changes` (which tracks content changes on competitor pages — different concern).

```
id                  BIGSERIAL PK
event_type          TEXT CHECK (
                       'new_entrant',
                       'drop_out',
                       'position_gain',      -- >= 3 positions up
                       'position_drop',      -- >= 3 positions down
                       'damco_position_change',
                       'damco_enters_top_n',
                       'damco_drops_top_n',
                       'serp_feature_appeared',
                       'serp_feature_disappeared',
                       'threat_tier_changed',
                       'first_seen_anywhere'
                    )
keyword_id          BIGINT FK → keywords(id) NULL  -- NULL for cross-keyword events
competitor_id       BIGINT FK → competitors(id) NULL  -- NULL for damco-only events
event_date          DATE
old_value           JSONB        -- e.g. {position: 7}
new_value           JSONB        -- e.g. {position: 3}
delta               INTEGER      -- normalized numeric delta where applicable
severity            TEXT CHECK ('critical', 'high', 'medium', 'low', 'info')
metadata            JSONB DEFAULT '{}'
created_at          TIMESTAMPTZ DEFAULT now()
```

Indexes: `(event_date DESC)`, `(keyword_id, event_date DESC)`, `(competitor_id, event_date DESC)`, `(severity, event_date DESC) WHERE severity IN ('critical', 'high')`.

### 5. New materialized view: `mv_offering_competition`

Offering-level rollup, refreshed at end of each snapshot cycle.

```sql
SELECT
  k.offering,
  c.id AS competitor_id,
  c.competitor_domain,
  c.company_name,
  c.category,
  c.threat_tier,
  COUNT(DISTINCT cr.keyword_id) AS keywords_in_top_10,
  COUNT(DISTINCT k.id) AS total_keywords_in_offering,
  ROUND(100.0 * COUNT(DISTINCT cr.keyword_id) / COUNT(DISTINCT k.id), 2) AS share_of_voice_pct,
  AVG(cr.rank_position) FILTER (WHERE cr.rank_position <= 10) AS avg_top_10_position,
  MIN(cr.rank_position) AS best_position,
  MAX(cr.date) AS last_seen_date
FROM keywords k
JOIN competitor_rankings cr ON cr.keyword_id = k.id
JOIN competitors c ON c.id = cr.competitor_id
WHERE k.status = 'active'
  AND cr.date = (SELECT MAX(date) FROM competitor_rankings WHERE keyword_id = cr.keyword_id)
  AND cr.rank_position <= 10
  AND c.is_tracked = TRUE
GROUP BY k.offering, c.id, c.competitor_domain, c.company_name, c.category, c.threat_tier
ORDER BY k.offering, share_of_voice_pct DESC;
```

This gives you, per offering: ranked list of competitors with share-of-voice, avg position, etc. Refresh after each snapshot run.

### 6. Optional second view: `mv_offering_competition_history`

Same shape, but bucketed by week — for trend lines. Lower priority; build if/when stakeholder reports need it.

---

## Update Conditions — When Does Data Get Written?

### A. Snapshot cycle (most writes happen here)

Trigger: cron OR on-demand OR new keyword added.

For each active keyword, the rank_tracker agent does:

1. **Fetch top 20 SERP** via DataForSEO connector
2. **Insert** into `keyword_serp_snapshots` (one row)
3. **Insert** into `competitor_rankings` (one row per result, up to top 20)
4. **Diff vs previous snapshot** — for each delta, insert into `competitor_serp_events`
5. **Upsert** `competitors`: new domain → insert; seen-before → update `last_seen_date`, recompute `keyword_appearance_count`, `offering_appearance_count`, `threat_tier`
6. **Refresh** materialized view at end of full cycle

### B. Manual curation (rare, human-in-loop)

- Setting `is_tracked = FALSE` for a domain you want to mute
- Editing `category`, `threat_tier`, `notes`, `metadata`
- Marking a domain `archived` (status column from existing schema)

### C. Backfill / one-off

- Initial population from a CSV or DataForSEO bulk pull
- Always logged in `agent_runs`

### Invariants

- **Never delete from `keyword_serp_snapshots` or `competitor_rankings`.** History is sacred.
- **Never delete from `competitor_serp_events`.** Append-only.
- **Never silently overwrite `competitors`.** Updates use UPSERT with explicit columns.
- **Every snapshot run is logged in `agent_runs`** with `records_processed = total rows written across all tables`.

---

## Event Severity Rules (for `competitor_serp_events.severity`)

| Event | Severity |
|-------|----------|
| `new_entrant` in positions 1-5 | high |
| `new_entrant` in positions 6-10 | medium |
| `new_entrant` in positions 11-20 | low |
| `drop_out` from positions 1-5 | medium (we want our competitor to drop, but track it) |
| `position_gain` >= 5 in top 10 | high |
| `position_gain` >= 3 in top 10 | medium |
| `damco_drops_top_n` | critical |
| `damco_enters_top_n` | high |
| `damco_position_change` >= 5 (drop) | high |
| `damco_position_change` >= 3 (drop) | medium |
| `damco_position_change` (gain) | info |
| `threat_tier_changed` to primary | high |
| `serp_feature_appeared` (AI Overview) | medium |
| `serp_feature_disappeared` | medium |

Severity drives downstream filtering — the alert agent only emails on `critical` + `high` by default.

---

## Threat Tier Computation Rules

`competitors.threat_tier` is recomputed at the end of each snapshot cycle:

```
primary    : keyword_appearance_count >= 5  AND category IN (direct, aggregator)
            OR offering_appearance_count >= 2 AND category = direct
watch      : keyword_appearance_count >= 2  AND category != ignore
peripheral : keyword_appearance_count = 1
ignore     : is_tracked = FALSE  OR  manually set to ignore
```

Tier changes emit `threat_tier_changed` events.

---

## Example Queries the System Will Answer

### "Who's competing with us in the AI offering right now?"

```sql
SELECT competitor_domain, share_of_voice_pct, avg_top_10_position, threat_tier
FROM mv_offering_competition
WHERE offering = 'AI'
ORDER BY share_of_voice_pct DESC
LIMIT 20;
```

### "Who's outranking us on 'ai consulting company'?"

```sql
SELECT cr.rank_position, c.competitor_domain, cr.url_found, cr.url_title, cr.is_new_entrant
FROM competitor_rankings cr
JOIN competitors c ON c.id = cr.competitor_id
JOIN keywords k ON k.id = cr.keyword_id
WHERE k.keyword = 'ai consulting company'
  AND cr.date = (SELECT MAX(date) FROM competitor_rankings WHERE keyword_id = k.id)
ORDER BY cr.rank_position;
```

### "What changed this week that's worth my attention?"

```sql
SELECT event_type, severity, keyword_id, competitor_id, old_value, new_value, event_date
FROM competitor_serp_events
WHERE event_date >= CURRENT_DATE - INTERVAL '7 days'
  AND severity IN ('critical', 'high')
ORDER BY event_date DESC, severity;
```

### "Which competitors are gaining on us across the AI offering?"

```sql
SELECT c.competitor_domain,
       COUNT(*) FILTER (WHERE e.event_type = 'position_gain') AS gains,
       COUNT(*) FILTER (WHERE e.event_type = 'new_entrant') AS new_entries
FROM competitor_serp_events e
JOIN competitors c ON c.id = e.competitor_id
JOIN keywords k ON k.id = e.keyword_id
WHERE k.offering = 'AI'
  AND e.event_date >= CURRENT_DATE - INTERVAL '30 days'
GROUP BY c.competitor_domain
ORDER BY gains + new_entries DESC
LIMIT 10;
```

---

## Open Questions for Review

Before I write the migration SQL, please confirm:

1. **Track top N — what's N?** Proposal: top 20 (so we capture striking-distance competitors and damco when not in top 10). Top 10 is also defensible if cost is a concern.
2. **Mobile vs desktop?** Schema supports both. Default desktop only? Both?
3. **Snapshot frequency for the full 1,112 keywords?** Weekly is the default; some keywords (high-priority AI ones) could be daily. Want a `snapshot_frequency_days` column on `keywords`?
4. **Drop the `offering` column from `competitors`?** Justification above (competitors span offerings). Confirm or push back.
5. **AI Overview citation tracking** — DataForSEO returns this in the AI Overview response object. Confirm we want to capture it (adds GEO visibility data).
6. **Materialized view refresh strategy** — REFRESH after every full cycle (synchronous, blocks until done) or CONCURRENTLY (non-blocking, requires unique index)? Recommend CONCURRENTLY.
7. **Retention** — keep all snapshots forever, or roll up into monthly aggregates after 12 months? Storage is cheap; default to keep forever.

Once these are settled I'll write `004_competition_tracking.sql` (migration) and update `keyword_intelligence/rank_tracker.py` to write to the new shape.
