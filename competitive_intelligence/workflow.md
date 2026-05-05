# Competitive Intelligence — Workflow Runbook

Runbook for the Competitive Intelligence Agent. **Most Python modules are not yet implemented** — but the SERP-side schema (migration 004) is in place and queries against it are available now.

## Decision tree

| User says / asks | Section | Status |
|---|---|---|
| "what changed on competitor sites", "weekly competitor digest" | [1. Competitor monitor](#1-competitor-monitor) | Planned |
| "competitor backlinks", "where are they getting links" | [2. Backlink analyzer](#2-backlink-analyzer) | Planned |
| "what are they publishing", "competitor blog tracker" | [3. Content monitor](#3-content-monitor) | Planned |
| "gap analysis", "what topics are we missing" | [4. Gap analyzer](#4-gap-analyzer) | Planned |
| "add / remove / mute a competitor", "manage competitor roster" | [5. Competitor roster](#5-competitor-roster) | Available |
| "show competitor SERP positions for our keywords" | [6. Query: competitor SERPs](#6-query-competitor-serps) | Available |
| "who's competing in the AI offering", "share of voice", "top competitors per offering" | [7. Query: offering rollup](#7-query-offering-rollup) | Available |
| "what changed in the SERP", "new entrants", "high-priority events", "weekly digest" | [8. Query: SERP event feed](#8-query-serp-event-feed) | Available |
| "who's outranking us right now", "displacement events" | [9. Query: damco displacement](#9-query-damco-displacement) | Available |
| "AI Overview citations", "GEO visibility per keyword" | [10. Query: AI Overview tracking](#10-query-ai-overview-tracking) | Available |

---

## 1. Competitor monitor

**Planned module:** `competitor_monitor.py`

**Behavior when built:** weekly crawl of each competitor's pillar and service pages. Diff against the last snapshot. Score significance of changes (title / H1 / new sections get higher scores). Write meaningful changes to `competitor_changes` and send an email digest.

**Planned command:** `python -m competitive_intelligence.competitor_monitor [--competitor domain.com]`

**Dependencies:** `common/connectors/crawler.py` (same one `technical_seo` needs).

---

## 2. Backlink analyzer

**Planned module:** `backlink_analyzer.py`

**Behavior when built:** pulls each competitor's backlinks via `common.connectors.dataforseo.get_backlinks()`, classifies by domain authority and niche, ranks platforms Damco should target. Produces a list for `offpage_links/`.

**Planned command:** `python -m competitive_intelligence.backlink_analyzer`

**Workaround today:** use the DataForSEO connector directly, store results in `backlinks` (with a marker that it's a competitor's).

---

## 3. Content monitor

**Planned module:** `content_monitor.py`

**Behavior when built:** tracks new URLs indexed under competitor domains (via DataForSEO or Google site:queries), classifies by content type (blog vs. landing), extracts topic, flags anything competing for Damco's target keywords.

**Planned command:** `python -m competitive_intelligence.content_monitor`

---

## 4. Gap analyzer

**Planned module:** `gap_analyzer.py`

**Behavior when built:** cross-references competitor page inventory and keyword coverage against Damco's. Uses `CLAUDE_MODEL_DEFAULT` to generate a narrative summary of strategic gaps ("Competitors A, B, C all have a glossary section covering terms X, Y, Z — Damco doesn't").

**Planned command:** `python -m competitive_intelligence.gap_analyzer --offering "AI Development"`

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

## What to always do

1. Present findings per competitor per offering — not a single giant list.
2. Route insights to the right agent: new keywords → `keyword_intelligence/`, new platforms → `offpage_links/`, new topics → `content_operations/`.
3. Log significant gap analysis findings to `memory/monitoring/`.
