# Off-Page & Links — Workflow Runbook

Runbook for the Off-Page & Links Agent. **Not yet implemented** — most sections are planning stubs.

## Decision tree

| User says / asks | Section | Status |
|---|---|---|
| "track backlinks", "monthly backlink pull", "update backlinks" | [1. Backlink tracker](#1-backlink-tracker) | **Available** |
| "find new outreach platforms", "where should we pitch" | [2. Platform finder](#2-platform-finder) | **Available** |
| "draft outreach for [platform]", "pitch email" | [3. Outreach drafter](#3-outreach-drafter) | **Available** |
| "draft a guest post about X", "UGC content for [platform]" | [4. Guest post drafter](#4-guest-post-drafter) | **Available** |
| "which platforms are worth the effort", "vendor performance" | [5. Vendor scorer](#5-vendor-scorer) | **Available** |
| "log this off-page activity", "published URL" | [6. Activity logging](#6-activity-logging) | Available |
| "show backlink growth", "how many links did we get" | [7. Query: backlink growth](#7-query-backlink-growth) | Available |

---

## 1. Backlink tracker

**Module:** `backlink_tracker.py` — **Available.**

Refreshes Damco's backlink inventory from DataForSEO + GSC. Idempotent — re-runs don't duplicate.

### Modes

| Flag | Behavior |
|---|---|
| (default) | All pages where `page_type IN ('pillar','service','home')` |
| `--page-id N` | One DB page |
| `--url URL` | One URL (must exist in `pages`) |
| `--domain DOMAIN` | Domain-level pull (resolved to the home URL in `pages`) |
| `--skip-gsc` | Skip GSC cross-check (avoids OAuth prompt) |
| `--limit N` | DataForSEO rows per target (default 1000) |
| `--dry-run` | Fetch + report; no DB writes |

### DataForSEO subscription

Backlinks API requires its own subscription (~$99/mo). When inactive, the module reports the access-denied error and continues with GSC-only mode (won't crash).

### Outputs

- `backlinks` upserted via `UNIQUE (source_url, page_id, data_source)`
- `outputs/audits/backlinks_<date>.md` — per-page table with new/existing counts, dofollow %, avg DA, GSC cross-confirmation flag

### Command

```bash
# Monthly cadence
python -m offpage_links.backlink_tracker

# One page
python -m offpage_links.backlink_tracker --page-id 42
```

---

## 2. Platform finder

**Module:** `platform_finder.py` — **Available.**

Mines `competitor_backlinks` to surface outreach prospects: domains linking to ≥2 tracked Damco competitors but not yet linking to Damco.

### Quality gates

- DA < 20 → drop (tunable via `--min-da`)
- Damco-own domains → drop
- Hardcoded blacklist (aggregators, socials, listing factories)
- Already in `platform_targets` with `status IN ('blacklist','exhausted')` → drop

### Scoring

`base_score = competitor_count * 10 + da_bonus + niche_relevance * 5 + recency_bonus`

`niche_relevance` is rule-based token overlap between the platform domain and Damco's offering vocabulary (defined in `OFFERING_TOKENS`).

### Output

Top N (default 50) → `platform_targets` with `status='pending'` for review. Markdown report at `outputs/audits/platforms_<date>.md`.

### Dependency

Requires `competitive_intelligence.backlink_analyzer` to have populated `competitor_backlinks`. When that table is empty, this module no-ops with a clear "blocked — subscription required" report.

### Command

```bash
# Top 50 prospects, all offerings
python -m offpage_links.platform_finder

# Tighter quality gate
python -m offpage_links.platform_finder --min-da 40 --min-competitors 3
```

---

## 3. Outreach drafter

**Module:** `outreach_drafter.py` — **Available.**

Drafts personalized pitch + 7-day follow-up for one platform → one Damco target page. **Never sends.**

### Inputs

| Flag | Behavior |
|---|---|
| `--platform-id N` (required) | `platform_targets.id` to pitch |
| `--target-page-id N` | Pitch this specific Damco page |
| `--offering "AI"` | Auto-pick the strongest page for this offering |
| `--no-crawl` | Skip the brief homepage fetch (faster, less personal) |
| `--no-llm` | Templated skeleton only |
| `--dry-run` | Write draft file; skip DB writes |

### Behavior

1. Refuses to draft if platform status is not `active` or `pending`.
2. Briefly crawls the platform's homepage to harvest editorial topics (H2s) — tunes the pitch to their actual coverage.
3. LLM (Sonnet, ~$0.02-0.05) produces `subject`, `body`, `followup`, `rationale`.
4. Saves to `outputs/outreach/`.
5. Inserts `offpage_activities` row with `activity_type='outreach'` + `status='draft'`.

### Safety

- Never sends.
- Conservative tone via system prompt — no promises of ranking outcomes.
- Rule-based skeleton fallback when Anthropic credit unavailable.

### Command

```bash
# Pitch to platform 7 for our AI service offering
python -m offpage_links.outreach_drafter --platform-id 7 --offering "AI"
```

---

## 4. Guest post drafter

**Module:** `guest_post_drafter.py` — **Available.**

Drafts an 800-1200 word guest post for a target platform on a specific topic. Includes inline compliance scan: word count, density, link-count, banned-claim phrases ("guaranteed", "fastest", "best", "#1").

### Inputs

| Flag | Behavior |
|---|---|
| `--platform-id N` (required) | `platform_targets.id` of the publication |
| `--topic "..."` | Free-text post topic |
| `--target-keyword "..."` | Keyword the post should rank/pass authority for |
| `--damco-target-url URL` | Damco URL to link to from the post |
| `--brief-id N` | Derive topic / keyword / URL from a `content_briefs` row |
| `--no-llm` | Structural skeleton only |
| `--no-crawl` | Skip platform homepage fetch |
| `--dry-run` | Write file; skip DB writes |

### Compliance scan (every draft)

- Word count: 800-1200 (warn outside)
- Inline link anchor MUST NOT be the bare target keyword (warn — looks spammy to editors)
- Exactly 1-2 Damco links (1 inline + optional 1 in bio); >2 → fail
- Keyword density 0.5-2.5% (warn outside, fail at 0%)
- ≥2 external citations with real URLs
- ≥5 H2 sections (warn below)
- No banned claim phrases (warn each occurrence)

Flags are inlined at the top of the markdown file so the editor sees them before reading.

### Output

`outputs/outreach/guest_posts/<platform-slug>_<kw-slug>_<date>.md` + `offpage_activities` row with `activity_type='guest_post'` + `status='draft'`.

### Command

```bash
# Drive from a content brief (most common)
python -m offpage_links.guest_post_drafter --platform-id 7 --brief-id 42

# Manual topic
python -m offpage_links.guest_post_drafter --platform-id 7 \
    --topic "Agentic AI architecture for insurance" \
    --target-keyword "ai agent development" \
    --damco-target-url https://www.damcogroup.com/ai-agent-development
```

---

## 5. Vendor scorer

**Module:** `vendor_scorer.py` — **Available.**

Aggregates `offpage_activities` per platform and rolls scores back into `platform_targets`. Pure SQL aggregation, no LLM.

### Metrics per platform

- `attempts`, `responses`, `publications`, `rejections`, `no_responses`, `still_draft`
- `response_rate = responses / attempts`
- `publication_rate = publications / attempts`
- `avg_turnaround_days` (first submit → first publish)
- `platform_da` (avg DA of backlinks they produced)
- `recency_score` (linear decay over 180 days)
- `quality_score = pub_rate*0.50 + resp_rate*0.25 + da_score*0.15 + recency*0.10`

### Auto-status mutations

- `response_rate < 10%` AND `attempts ≥ 5` AND `publications == 0` → status becomes `exhausted`
- `blacklist` / already-`exhausted` platforms are never auto-resurrected

### Outputs

- `platform_targets` updates: `response_rate`, `quality_score`, `last_contacted`, optional `status` flip
- `outputs/audits/vendor_scores_<date>.md` — top performers, status changes, near-exhaustion warnings
- `outputs/reports/vendor_scores_<date>.xlsx` — sortable data

### Command

```bash
# Score everything
python -m offpage_links.vendor_scorer

# Tune the exhaust threshold
python -m offpage_links.vendor_scorer --exhaust-below 15

# Preview without mutating platform_targets
python -m offpage_links.vendor_scorer --dry-run
```

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
