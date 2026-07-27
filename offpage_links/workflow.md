# Off-Page & Links — Workflow Runbook

Runbook for the Off-Page & Links Agent. All five modules are written. **None
has ever logged a run**, and one cannot currently start from the command line —
read [§0 Before you run anything](#0-before-you-run-anything) first.

## Decision tree

| User says / asks | Section | Status |
|---|---|---|
| "is any of this working", "what's blocked" | [0. Before you run anything](#0-before-you-run-anything) | Available |
| "track backlinks", "monthly backlink pull", "update backlinks" | [1. Backlink tracker](#1-backlink-tracker) | **CLI broken** + subscription-blocked |
| "find new outreach platforms", "where should we pitch" | [2. Platform finder](#2-platform-finder) | Runs; no input data yet |
| "draft outreach for [platform]", "pitch email" | [3. Outreach drafter](#3-outreach-drafter) | Runs once `platform_targets` has rows |
| "draft a guest post about X", "UGC content for [platform]" | [4. Guest post drafter](#4-guest-post-drafter) | Runs once `platform_targets` has rows |
| "which platforms are worth the effort", "vendor performance" | [5. Vendor scorer](#5-vendor-scorer) | Runs; needs activity history to say anything |
| "log this off-page activity", "published URL" | [6. Activity logging](#6-activity-logging) | Available |
| "show backlink growth", "how many links did we get" | [7. Query: backlink growth](#7-query-backlink-growth) | Available |

---

## 0. Before you run anything

### Prerequisite: migrations

Every module in this folder resolves the client's identity through
`common/tenant.py`, which reads the `tenant*` tables from migration 012.
Without a tenant row, `profile()` raises `TenantNotConfigured` and the module
stops before doing any work. This is deliberate — the alternative is drafting
someone else's pitch under our name.

```bash
python sql/migrate.py
```

Migration 012 seeds the tenant, its domains, offerings, vocabularies
(`domain_blacklist`, `analyst_sources`, `banned_claims`, `banned_openers`) and
policies (`content_style`). Nothing in this folder works until it is applied.

### Check status before promising anyone a number

```bash
python -m common.agents --folder offpage_links
```

Prints each module with its last run date, last status, and any registry-level
blocker. Today all five read `never`; `backlink_tracker` and `platform_finder`
carry `[BLOCKED: DataForSEO Backlinks subscription]`.

### The dependency chain

```
DataForSEO Backlinks subscription
  └─ competitive_intelligence.backlink_analyzer  → competitor_backlinks
       └─ platform_finder                        → platform_targets
            ├─ outreach_drafter                  → offpage_activities
            └─ guest_post_drafter                → offpage_activities
                 └─ vendor_scorer                → platform_targets (scores)
```

`competitor_backlinks` is empty and `backlink_analyzer` last ran with status
`error`. Everything downstream is starved. One subscription unblocks the chain.

### Historical figures you should not quote

`vendor_scorer` used to count `submitted` as a response, so any
**response-rate number produced before 2026-07-27 is inflated** — a pitch we
sent and never heard back on was scored as answered. The auto-`exhausted` rule
could essentially never fire as a result. Re-run the scorer before citing a
response rate anywhere.

Separately, `backlinks.domain_authority` used to store DataForSEO's raw
0-1000 `rank` while every consumer clamps to 100, so **every platform scored a
perfect 100 DA**. Normalization now happens at the connector. Rows written
before today are on the old scale.

---

## 1. Backlink tracker

**Module:** `backlink_tracker.py`

Refreshes our backlink inventory from DataForSEO + GSC. Idempotent — re-runs
don't duplicate, thanks to `UNIQUE (source_url, page_id, data_source)`.

### ⚠️ The CLI does not start

`main()` interpolates `profile().brand_name` into the argparse description but
the module never imports `profile`. Every invocation, `--help` included, exits
with:

```
NameError: name 'profile' is not defined. Did you forget to import 'profile'?
```

`run()` is importable and functional — only the command-line entry point is
broken. Fix the import in `main()` before the subscription lands; there is no
point scheduling this module until then.

### Modes

| Flag | Behavior |
|---|---|
| (no target flag) | All pages where `page_type IN ('pillar','service','home')` |
| `--page-id N` | One DB page |
| `--url URL` | One URL (must already exist in `pages`) |
| `--domain DOMAIN` | Domain-level pull, resolved to a home URL that must exist in `pages` |
| `--limit N` | DataForSEO rows per target (default 1000) |
| `--skip-gsc` | Skip the GSC cross-check (avoids the OAuth prompt) |
| `--dry-run` | Fetch + report; no DB writes |
| `--verbose` / `-v` | Debug logging |

`--page-id`, `--url` and `--domain` are mutually exclusive.

### What GSC actually contributes

Search Console's public API does not expose external link sources. This module
uses Search Analytics with `dimensions=[page]` to learn which of our pages saw
activity in the last 30 days, and marks a DataForSEO-discovered backlink as
GSC-cross-confirmed when its target page appears there. That is weak
confirmation, not independent discovery — do not describe it as a second
backlink source in a report to anyone.

### DataForSEO subscription

The Backlinks API needs its own subscription (~$99/mo). When it is inactive the
module records the access-denied error per page, sets the "blocked" line in the
report, and continues rather than crashing.

### Outputs

- `backlinks` rows upserted. `domain_authority` is stored 0-100.
- `outputs/audits/backlinks_<date>.md` — per-page table with new/existing counts, distinct domains, dofollow counts, avg DA, GSC cross-confirmation flag.

**Caveat on that report:** the "Avg DA" column averages the connector's raw
`rank` (0-1000), not the normalized value it writes to the database. The column
reads roughly 10x high. The stored data is correct.

### Command

```bash
# Monthly cadence — every pillar, service and home page
python -m offpage_links.backlink_tracker

# One page
python -m offpage_links.backlink_tracker --page-id 42

# Domain-level pull
python -m offpage_links.backlink_tracker --domain example.com

# No OAuth prompt
python -m offpage_links.backlink_tracker --skip-gsc --dry-run
```

---

## 2. Platform finder

**Module:** `platform_finder.py` — repaired today; never run.

Mines `competitor_backlinks` to surface outreach prospects: domains linking to
≥2 tracked competitors but not yet linking to us.

**Before today this module could not have completed a single run.** It selected
`domain_authority`, `anchor` and `dofollow` from `competitor_backlinks` — the
columns are `domain_rank`, `anchor_text` and `is_dofollow` — and joined
`competitors.offering`, which migration 004 dropped. Offerings are now derived
from the keywords each competitor ranks for, aggregated once in a CTE.

### Quality gates

- Fewer than `--min-competitors` distinct competitors linking → drop
- Average DA below `--min-da` → drop
- Our own domains, via `profile().owns()` → drop
- Domains in the tenant's `domain_blacklist` vocabulary → drop
- Already in `platform_targets` with `status IN ('blacklist','exhausted')` → drop

The blacklist is data now, not a Python literal. Add junk you see in the report
to `tenant_vocabularies` under `kind='domain_blacklist'` — aggressively.

### Scoring

```
score = competitor_count * 10
      + max(0, (avg_da - 30) / 5)
      + niche_relevance * 5
      + 3 if linked within 90 days
```

`niche_relevance` (0-3) is rule-based token overlap between the platform domain
and the tenant's per-offering niche tokens from the profile. The total is
clamped at **999.99** — `platform_targets.quality_score` is `NUMERIC(5,2)`, and
an aggregator linking to a hundred competitors would otherwise overflow the
upsert. Clamping happens in the scorer so the report and the stored value agree.

### `--offering` is free text

It is matched against `keywords.offering` in SQL, so it can name something the
tenant profile has no offering row for. When that happens the module logs a
warning and every candidate scores `niche_relevance = 0.0`, leaving competitor
count and DA to decide the ranking. Check the offering name against
`profile().offering_names` if the scores look flat.

### Output

Top N (default 50) upserted into `platform_targets` with `status='pending'`.
Report at `outputs/audits/platforms_<date>[_<offering>].md`.

### Dependency

Requires `competitive_intelligence.backlink_analyzer` to have populated
`competitor_backlinks`. When that table is empty the module writes a
"⚠️ Blocked" report, logs the run as `partial`, and exits without touching
`platform_targets`.

### Command

```bash
# Top 50 prospects, all offerings
python -m offpage_links.platform_finder

# Tighter quality gate
python -m offpage_links.platform_finder --min-da 40 --min-competitors 3

# One offering, more rows persisted
python -m offpage_links.platform_finder --offering "AI" --top-n 100

# Analyze + report, no DB writes
python -m offpage_links.platform_finder --dry-run
```

Flags: `--offering`, `--min-da` (20), `--min-competitors` (2), `--top-n` (50),
`--dry-run`, `--verbose`.

---

## 3. Outreach drafter

**Module:** `outreach_drafter.py` — never run.

Drafts a personalized pitch + 7-day follow-up for one platform → one of our
target pages. **Never sends.**

### Inputs

| Flag | Behavior |
|---|---|
| `--platform-id N` | **Required.** `platform_targets.id` to pitch |
| `--target-page-id N` | Pitch this specific page |
| `--offering "AI"` | Auto-pick the strongest page for this offering |
| `--no-crawl` | Skip the platform homepage fetch (faster, less personal) |
| `--no-llm` | Templated skeleton only |
| `--dry-run` | Write the draft file; skip DB writes |
| `--verbose` / `-v` | Debug logging |

`--target-page-id` and `--offering` are mutually exclusive and **exactly one is
required** — the command will not parse without one of them.

### Behavior

1. Refuses to draft when the platform's status is not `active` or `pending`.
2. Briefly crawls the platform homepage to harvest editorial topics (title, H1, first 8 H2s) so the pitch matches their actual coverage.
3. Claude (Sonnet, ~$0.02-0.05) returns `subject`, `body`, `followup`, `rationale`.
4. Saves to `outputs/outreach/<platform-slug>_<page-slug>_<date>.md`.
5. Inserts an `offpage_activities` row with `activity_type='outreach'`, `status='draft'`.

### Identity handling

The user prompt is tenant-neutral by construction. Brand identity enters only
through `system_preamble()`, passed as `system=`. The rule-based fallback —
which is the path taken whenever Anthropic credit is unavailable, so it ships on
real drafts — signs off with the tenant's brand name and CTA URL from the
profile, not a baked-in company.

### Read the rationale skeptically

The prompt asks the model to justify why this platform fits this page, but
`platform_finder` already computed competitor count, average DA, niche relevance
and recency onto `platform_targets` and none of it is passed to the model. The
rationale is a plausible guess, not analysis. Check the numbers on the
`platform_targets` row yourself.

### Safety

- Never sends.
- Conservative tone enforced in the system prompt: no promises of ranking outcomes, no fabricated stats.
- Rule-based skeleton fallback when Anthropic credit is unavailable.

### Command

```bash
# Pitch platform 7 for our AI offering
python -m offpage_links.outreach_drafter --platform-id 7 --offering "AI"

# Pitch a specific page, no crawl
python -m offpage_links.outreach_drafter --platform-id 7 --target-page-id 42 --no-crawl
```

---

## 4. Guest post drafter

**Module:** `guest_post_drafter.py` — repaired today; never run.

Drafts a full third-party article for a target publication, with the brand CTA
URL embedded contextually, then runs a 10-check rule-based compliance scan over
the result. **Never publishes.**

### ⚠️ A failed run still writes a file

This is the thing an operator has to know. When generation degrades — LLM
disabled, Anthropic credit unavailable, unparseable JSON, or the model's
response truncated mid-object — the module writes a rule-based skeleton full of
`[FILL: ...]` markers to the normal output path, exactly where a real draft
would land.

What now makes that visible:

- The `agent_runs` row is logged **`failed`**, with `records_processed=0` and the reason in `errors`.
- The markdown file opens with a **`## ❌ NOT A USABLE DRAFT`** banner, above the compliance flags, plus the first 500 characters of the raw LLM response for diagnosis.
- The console prints `*** NOT A USABLE DRAFT ***`.

Previously this shipped labelled complete. A skeleton trivially passes checks it
was never subject to, so a clean compliance scan on a degraded file means
nothing. **If the banner is there, do not send the file.**

Truncation is detected separately from a parse failure — the reason in the log
is the real one, and for truncation it tells you to raise the output budget or
narrow the word band. The budget scales with the requested word count
(`max(16000, word_count_max * 4 + 4000)` tokens); the old flat 8000 truncated
long-form articles routinely.

### Inputs

| Flag | Behavior |
|---|---|
| `--platform-id N` | **Required.** `platform_targets.id` of the publication |
| `--topic "..."` | Free-text topic / angle |
| `--target-keyword "..."` | Primary keyword the post should rank for |
| `--brand-target-url URL` | Our own page to link to from the post (the brand CTA URL) |
| `--damco-target-url URL` | **Deprecated** alias for `--brand-target-url`. Still honoured so existing scripts keep working; hidden from `--help`. Use the new name. |
| `--brief-id N` | Derive topic / primary keyword / secondary keywords / target URL from a `content_briefs` row |
| `--blog-title "..."` | Final blog title (otherwise derived from the target keyword) |
| `--secondary-keywords "a,b,c"` | Comma-separated |
| `--target-audience "..."` | Defaults to the tenant's audience descriptor |
| `--brand-name "..."` | Defaults to the tenant's brand name |
| `--word-count-min N` / `--word-count-max N` | Body word band (default 800-1200); min must be ≤ max |
| `--perspective "..."` | Defaults to the `content_style` policy |
| `--reference-url URL` | Reference article to read first; drives the content-gap section |
| `--max-em-dashes N` | Defaults to the `content_style` policy |
| `--cta-link-min N` / `--cta-link-max N` | Brand CTA links in the body (default 1-3) |
| `--no-crawl` | Skip the platform homepage fetch |
| `--no-llm` | Structural skeleton only — this is a degraded run and is logged `failed` |
| `--dry-run` | Write the file; skip DB writes |
| `--verbose` / `-v` | Debug logging |

`--topic`, `--target-keyword` and `--brand-target-url` are all required unless
`--brief-id` supplies them. A relative `--brand-target-url` is resolved against
the tenant's CTA URL (or primary domain).

### House style comes from the profile

Em-dash cap, keyword-density band, perspective, English variant and style guide
come from the `content_style` policy. Acceptable statistic sources come from the
`analyst_sources` vocabulary. The banned-claim and banned-opener lists are regex
sources in `banned_claims` / `banned_openers`. Change what a client will and
won't print by editing those rows, not the module. A malformed regex is logged
and skipped rather than losing a paid LLM call.

### Compliance scan (10 checks, every draft, all rule-based)

| # | Check | Severity |
|---:|---|---|
| 1 | Body word count inside the configured band | warn, or fail beyond ±30% |
| 2 | Primary keyword density inside the `content_style` band | warn; fail at 0% or >1.5x the ceiling |
| 3 | Brand CTA link count inside `--cta-link-min/max` | **fail** at 0; warn outside the band |
| 4 | Inline CTA anchor is not the bare primary keyword | warn |
| 5 | Em dashes (U+2014 only) at or under the cap | warn |
| 6 | No banned claim / cliché phrases | warn per pattern |
| 7 | No default-AI paragraph openers | warn, first 5 |
| 8 | At least 5 H2 sections | warn |
| 9 | At least 2 external citations with real `http` URLs | warn |
| 10 | Conclusion 100-125 words | warn |

Flags are written at the top of the markdown file so the editor sees them before
reading. Run status: `failed` when degraded, `partial` when any check fails,
`success` otherwise.

### Keyword frequency table

The prompt asks the model for its own counts, but LLMs cannot count reliably and
this table is what a human reviews before publishing. The artifact now shows the
**measured** count for each keyword, with the model's claim beside it marked ⚠
where the two disagree.

### Output

`outputs/outreach/guest_posts/<platform-slug>_<kw-slug>_<date>.md`, plus an
`offpage_activities` row with `activity_type='guest_post'`, `status='draft'`.

### Command

```bash
# Drive from a content brief (most common)
python -m offpage_links.guest_post_drafter --platform-id 7 --brief-id 42

# Manual topic
python -m offpage_links.guest_post_drafter --platform-id 7 \
    --topic "Agentic AI architecture for insurance" \
    --target-keyword "ai agent development" \
    --brand-target-url https://www.damcogroup.com/ai-agent-development

# Long-form third-party article
python -m offpage_links.guest_post_drafter --platform-id 7 --brief-id 42 \
    --word-count-min 2000 --word-count-max 2500 \
    --blog-title "How Data Validation Became the Backbone of Agentic AI" \
    --reference-url https://example.com/related-article
```

---

## 5. Vendor scorer

**Module:** `vendor_scorer.py` — repaired today; never run.

Aggregates `offpage_activities` per platform and rolls the scores back into
`platform_targets`. Pure SQL aggregation plus Python tabulation — no LLM, no API
calls.

### ⚠️ Response rate was previously wrong

`submitted` means we sent it, not that they replied. It used to be counted as a
response, which made every ignored pitch look answered. `responses` now excludes
`draft`, `submitted` and `no_response`. Two consequences:

- **Every response-rate figure recorded before today reads high.** Re-run the scorer before quoting one.
- The auto-`exhausted` rule could essentially never fire, so no platform was ever retired automatically. Expect the first corrected run to propose retirements that should have happened months ago — review them before accepting.

### Metrics per platform

- `attempts`, `responses`, `publications`, `rejections`, `no_responses`, `still_draft`, `submitted`
- `response_rate = responses / attempts`
- `publication_rate = publications / attempts`
- `avg_turnaround_days` — approximated as first `submitted` → first `published` per platform; there are no per-activity status-transition timestamps
- `platform_da` — average DA of the backlinks that platform produced, clamped 0-100. The clamp is a safety net now, not a scaler: the connector normalizes DataForSEO's raw 0-1000 rank. It used to saturate every platform at 100.
- `recency_score` — 100 within 30 days, decaying linearly to 0 at 180
- `quality_score = pub_rate*0.50 + resp_rate*0.25 + da_score*0.15 + recency*0.10`

The DA join is best-effort; if it fails the run continues without the DA
component rather than aborting.

### Auto-status mutations

- `response_rate < --exhaust-below` **and** `attempts ≥ 5` **and** `publications == 0` → status becomes `exhausted`
- `blacklist` and already-`exhausted` platforms are never auto-resurrected
- Platforms with zero attempts are left alone — we haven't really tried

### Outputs

- `platform_targets` updates: `response_rate`, `quality_score`, `last_contacted`, and `status` on a flip
- `outputs/audits/vendor_scores_<date>.md` — top performers, status changes, near-exhaustion warnings, inactive platforms
- `outputs/reports/vendor_scores_<date>.xlsx` — sortable data

### Command

```bash
# Score everything
python -m offpage_links.vendor_scorer

# Only status='active' platforms
python -m offpage_links.vendor_scorer --only-active

# Tune the exhaust threshold
python -m offpage_links.vendor_scorer --exhaust-below 15

# Preview without mutating platform_targets — do this first, given the fix above
python -m offpage_links.vendor_scorer --dry-run
```

---

## 6. Activity logging

**Available now.** Insert directly to `offpage_activities`:

```sql
INSERT INTO offpage_activities
    (executive, activity_type, target_page_id, platform_id, platform,
     status, date, published_url, notes)
VALUES (%s, %s, %s, %s, %s, %s, CURRENT_DATE, %s, %s);
```

Valid `activity_type`: `guest_post`, `ugc`, `outreach`, `pr_pitch`, `paid_placement`, `follow_up`, `other`.
Valid `status`: `draft`, `submitted`, `published`, `rejected`, `no_response`.

Set `platform_id` where you can — `vendor_scorer` joins on it, and a row without
it never contributes to any platform's score. And keep `submitted` meaning
"we sent it": the scorer now depends on that distinction.

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

Returns zero everywhere until `backlink_tracker` runs successfully for the first
time.

---

## What to always do

1. Apply migrations before anything else. No tenant row, no run.
2. Check `python -m common.agents --folder offpage_links` before reporting status. Do not read it off a markdown table.
3. De-duplicate by `source_url` when counting backlinks — DataForSEO and GSC each get their own row.
4. Every outreach and guest-post draft is saved AND logged as an `offpage_activities` row with status `draft`.
5. Check for the "NOT A USABLE DRAFT" banner before forwarding anything `guest_post_drafter` produced.
6. Never auto-send, auto-publish, or skip the human review step.
