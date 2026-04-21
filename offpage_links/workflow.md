# Off-Page & Links — Workflow Runbook

Runbook for the Off-Page & Links Agent. **Not yet implemented** — most sections are planning stubs.

## Decision tree

| User says / asks | Section | Status |
|---|---|---|
| "track backlinks", "monthly backlink pull", "update backlinks" | [1. Backlink tracker](#1-backlink-tracker) | Planned (Phase 1) |
| "find new outreach platforms", "where should we pitch" | [2. Platform finder](#2-platform-finder) | Planned (Phase 2) |
| "draft outreach for [platform]", "pitch email" | [3. Outreach drafter](#3-outreach-drafter) | Planned (Phase 3) |
| "draft a guest post about X", "UGC content for [platform]" | [4. Guest post drafter](#4-guest-post-drafter) | Planned (Phase 3) |
| "which platforms are worth the effort", "vendor performance" | [5. Vendor scorer](#5-vendor-scorer) | Planned (Phase 2) |
| "log this off-page activity", "published URL" | [6. Activity logging](#6-activity-logging) | Available |
| "show backlink growth", "how many links did we get" | [7. Query: backlink growth](#7-query-backlink-growth) | Available |

---

## 1. Backlink tracker

**Planned module:** `backlink_tracker.py`

**Behavior when built:**
- Monthly pull via `common.connectors.dataforseo.get_backlinks(target)` for each Damco pillar page.
- Parallel pull via `common.connectors.gsc.get_search_analytics` with `dimensions=["page"]` to catch GSC-only discoveries.
- Merge on `source_url`; prefer DataForSEO's DA score. Tag `data_source` correctly.
- Upsert into `backlinks` with `ON CONFLICT (source_url, page_id, data_source)`.

**Planned command:** `python -m offpage_links.backlink_tracker [--page-id N]`

**Workaround today:**
```python
from common.connectors.dataforseo import get_backlinks
results = get_backlinks("damcogroup.com/ai-agent-development", limit=1000)
# Then upsert to `backlinks` table
```

---

## 2. Platform finder

**Planned module:** `platform_finder.py`

**Behavior when built:**
- Reads the `competitors` table, pulls each competitor's top backlinks.
- Filters out: Damco's own domains, DA < 20, known spam/PBN lists.
- Scores each candidate platform by niche relevance, DA, editorial style match.
- Writes candidates to `platform_targets` with quality scores.

**Planned command:** `python -m offpage_links.platform_finder --offering "AI Development"`

---

## 3. Outreach drafter

**Planned module:** `outreach_drafter.py`

**Behavior when built:**
- Takes a `platform_target` ID and a Damco offering or target page.
- Fetches the target platform's recent content to tune tone.
- Uses `CLAUDE_MODEL_DEFAULT` to draft a personalized pitch (subject line + body + 1 follow-up variant).
- Saves to `outputs/outreach/`, creates a `offpage_activities` row with status `draft`.

**Planned command:** `python -m offpage_links.outreach_drafter --platform-id 42 --target-page-id 7`

**Safety:** Never sends. Drafts only. Executive must approve and send.

---

## 4. Guest post drafter

**Planned module:** `guest_post_drafter.py`

**Behavior when built:**
- Takes a topic (from content brief or ad-hoc), a target platform, and target keywords.
- Uses `CLAUDE_MODEL_DEFAULT` to draft an 800–1200 word guest post matching the platform's editorial style.
- Saves to `outputs/outreach/` as a .docx.
- Tags compliance: word count, keyword density, link to Damco (1–2 max, contextual).

**Planned command:** `python -m offpage_links.guest_post_drafter --platform-id 42 --topic "agentic AI architecture" --target-keyword "ai agent development"`

---

## 5. Vendor scorer

**Planned module:** `vendor_scorer.py`

**Behavior when built:**
- Reads `platform_targets` + historical `offpage_activities` per platform.
- Computes: response rate, publication rate, turnaround time, link quality score.
- Updates `platform_targets.response_rate` and `quality_score`.
- Flags platforms with response_rate < 10% as `exhausted`.

---

## 6. Activity logging

**Available now.** Insert directly to `offpage_activities`:

```sql
INSERT INTO offpage_activities
    (executive, activity_type, target_page_id, platform, status, date, published_url, notes)
VALUES (%s, %s, %s, %s, %s, CURRENT_DATE, %s, %s);
```

Valid `activity_type`: `guest_post`, `ugc`, `outreach`, `pr_pitch`, `paid_placement`, `follow_up`, `other`.
Valid `status`: `draft`, `submitted`, `published`, `rejected`, `no_response`.

---

## 7. Query: backlink growth

**Available now.**

```sql
SELECT p.url,
       count(*) FILTER (WHERE b.date_discovered >= CURRENT_DATE - INTERVAL '30 days') AS new_30d,
       count(*) FILTER (WHERE b.date_discovered >= CURRENT_DATE - INTERVAL '90 days') AS new_90d,
       count(*) AS total
FROM pages p
LEFT JOIN backlinks b ON b.page_id = p.id
WHERE p.page_type IN ('pillar', 'service')
GROUP BY p.url
ORDER BY new_30d DESC NULLS LAST;
```

---

## What to always do

1. De-duplicate backlinks when merging DataForSEO + GSC.
2. Every outreach draft gets saved AND logged as an `offpage_activities` row with status `draft`.
3. Never auto-send, auto-publish, or skip the human review step.
