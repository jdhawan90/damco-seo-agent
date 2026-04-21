# Competitive Intelligence — Workflow Runbook

Runbook for the Competitive Intelligence Agent. **Not yet implemented** — most sections are planning stubs. Offer to implement or run ad-hoc equivalents.

## Decision tree

| User says / asks | Section | Status |
|---|---|---|
| "what changed on competitor sites", "weekly competitor digest" | [1. Competitor monitor](#1-competitor-monitor) | Planned |
| "competitor backlinks", "where are they getting links" | [2. Backlink analyzer](#2-backlink-analyzer) | Planned |
| "what are they publishing", "competitor blog tracker" | [3. Content monitor](#3-content-monitor) | Planned |
| "gap analysis", "what topics are we missing" | [4. Gap analyzer](#4-gap-analyzer) | Planned |
| "add a competitor", "remove a competitor" | [5. Competitor roster](#5-competitor-roster) | Available |
| "show competitor SERP positions for our keywords" | [6. Query: competitor SERPs](#6-query-competitor-serps) | Available |

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

**Available now.** Read/write the `competitors` table directly.

Add a competitor:
```sql
INSERT INTO competitors (competitor_domain, offering, status)
VALUES ('appinventiv.com', 'AI Development', 'active')
ON CONFLICT (competitor_domain) DO NOTHING;
```

List active competitors:
```sql
SELECT competitor_domain, offering, status, date_last_crawled
FROM competitors ORDER BY offering, competitor_domain;
```

Initial roster (from `../memory/monitoring/2026-04-14-damcogroup-rank-tracking-setup.md`):
- AI Development: appinventiv.com, itransition.com, coherentsolutions.com, leewayhertz.com, effectivesoft.com

---

## 6. Query: competitor SERPs

**Available now** — read from `competitor_rankings`.

```sql
SELECT c.competitor_domain, k.keyword, cr.rank_position, cr.date
FROM competitor_rankings cr
JOIN competitors c ON c.id = cr.competitor_id
JOIN keywords k ON k.id = cr.keyword_id
WHERE cr.date = (SELECT max(date) FROM competitor_rankings WHERE competitor_id = c.id)
  AND k.keyword = %s
ORDER BY cr.rank_position;
```

---

## What to always do

1. Present findings per competitor per offering — not a single giant list.
2. Route insights to the right agent: new keywords → `keyword_intelligence/`, new platforms → `offpage_links/`, new topics → `content_operations/`.
3. Log significant gap analysis findings to `memory/monitoring/`.
