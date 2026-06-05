# Content Operations — Workflow Runbook

Runbook for the Content Operations Agent. **Not yet implemented** — most sections are planning stubs.

## Decision tree

| User says / asks | Section | Status |
|---|---|---|
| "generate a brief", "create content briefs from gap_analyzer", "what should writers work on" | [1. Brief generation](#1-brief-generation) | **Available** |
| "check this content against the brief", "run compliance" | [2. Compliance check](#2-compliance-check) | **Available** |
| "what glossary pages are missing", "definition-intent gaps", "AEO opportunities" | [3. Glossary detection](#3-glossary-detection) | **Available** |
| "are we over-concentrated on X", "content calendar balance" | [4. Concentration check](#4-concentration-check) | **Available** |
| "show open briefs", "brief status" | [5. Query: brief pipeline](#5-query-brief-pipeline) | Available |
| "show compliance history for page X" | [6. Query: compliance history](#6-query-compliance-history) | Available |

---

## 1. Brief generation

**Module:** `brief_generator.py` — **Available now.**

Takes a target keyword (or set of keywords) and emits a complete SEO content brief — the document a writer needs to draft a ranking page. Designed to chain off `competitive_intelligence.gap_analyzer`.

### Modes

| Flag | Behavior |
|---|---|
| `--coverage-gap` | Auto-picks coverage-gap keywords (Damco missing from top 100, ≥1 tracked competitor in top 10), ranked by GSC impressions. The primary mode. |
| `--keyword-ids 42,43,45` | Manual: brief for these specific keyword IDs |
| `--offering "AI"` | Restrict coverage-gap pool to one offering |
| `--limit N` | Cap on coverage-gap briefs per run (default: 10) |
| `--no-llm` | Force rule-based output (skip Claude even if available) |
| `--dry-run` | Write markdown brief to disk but skip DB inserts |

### What's in each brief

| Section | How it's built |
|---|---|
| Primary keyword + suggested URL | Slug derived from keyword |
| GSC demand (14d) | clicks, impressions, position from `keyword_rankings` |
| Audience stage (awareness / consideration / decision) | Rule-based heuristic on keyword wording (cf. "what is X" = awareness, "X pricing" = decision) |
| Secondary keywords | Lexical-overlap scoring across all keywords in the same offering, ranked by GSC demand |
| Top 5 competitor reference URLs | From `competitor_rankings` — the SERP we need to outrank |
| Heading outline | Template skeleton refined by LLM into 6-8 specific H2s |
| Must-include subtopics + buyer questions | LLM-generated from competitor context |
| Internal linking suggestions | Topical match against `pages` (audited via site_auditor). Generic tokens like "services" / "solutions" / "company" are explicitly excluded from matching so we don't false-positive on every service page. |
| Narrative angle (intro hook / topic angle / unique POV) | LLM, with `[PLACEHOLDER]` markers when Anthropic credit isn't available |
| **AEO checklist** | Hardcoded — present in every brief regardless of LLM availability |
| Recommended word count | Page-type-aware (service: 1000, pillar: 1500, blog: 800, etc.) |

### Outputs

- **DB:** one `content_briefs` row per brief with `status='draft'`, `brief_content` (JSONB), `target_url`, `file_path`, `keywords_json`
- **Disk:** `outputs/briefs/<slug>_<date>.md` — writeable markdown brief ready to hand to a writer

### LLM behavior

Uses `common.llm.call_claude` with tier `default` (sonnet). One call per brief (~2k in / ~1.5k out / ~$0.02-0.05). On `LLMUnavailableError`: narrative sections show `[PLACEHOLDER — load Anthropic credit and re-run]` markers; structured sections still populate fully.

### Command

```bash
# Top 10 coverage gaps across all offerings, full LLM enrichment
python -m content_operations.brief_generator --coverage-gap --limit 10

# Top 5 coverage gaps in AI offering only
python -m content_operations.brief_generator --coverage-gap --offering "AI" --limit 5

# Manual: specific keyword cluster
python -m content_operations.brief_generator --keyword-ids 42,43,45

# Rule-based only (no LLM cost / when credit isn't loaded)
python -m content_operations.brief_generator --coverage-gap --limit 5 --no-llm

# Preview without DB inserts
python -m content_operations.brief_generator --coverage-gap --limit 3 --dry-run
```

### Cost

LLM: ~$0.02-0.05 per brief with Sonnet. A 10-brief coverage-gap batch costs ~$0.20-0.50.
Without `--no-llm` and without Anthropic credit: $0 (rule-based output, `[PLACEHOLDER]` markers in narrative sections).

### Validation (2026-05-28)

- Coverage-gap mode picked the right 3 BPM targets including `data enrichment services` (the keyword we analyzed manually earlier).
- Brief for `data enrichment services` showed:
  - GSC demand correctly: 431 impressions, 2 clicks, avg position 29
  - 8 secondary keywords scored by lexical overlap with the primary
  - Top 5 competitors: blackbaud.com, snov.io, zapier.com, alation.com, edq.com (matches manual SERP check from earlier session)
  - AEO checklist with 9 items
  - Audience stage = consideration (service-class keyword)
- DB write verified: `content_briefs` row #1 created with `status='draft'`, file_path populated, agent_runs logged.

---

## 2. Compliance check

**Module:** `compliance_checker.py` — **Available now.**

Crawls a submitted draft URL and scores it against the brief that was generated for the same target. 12 weighted dimensions, 0–100 score, per-issue pass/warn/fail. Pure rule-based — no LLM cost.

### Dimensions checked

| Dimension | Weight | What's measured |
|---|---:|---|
| `primary_keyword_placement` | 18 | Primary kw in title, H1, meta, first 100 words |
| `primary_keyword_density`   | 8  | Body density in 0.5–3.0% band |
| `secondary_keyword_coverage`| 8  | Each brief secondary kw appears ≥1× |
| `title_length`              | 6  | 50–60 chars ideal, 30–70 acceptable |
| `meta_description`          | 8  | 140–160 chars ideal |
| `h1_structure`              | 6  | Exactly one H1 |
| `outline_coverage`          | 8  | Brief H2s + must-include subtopics show up |
| `internal_links`            | 8  | ≥3 internal links + brief-suggested targets present |
| `image_alt_text`            | 6  | ≥80% body images have alt |
| `schema_markup`             | 6  | JSON-LD present; FAQPage = bonus |
| `word_count`                | 8  | ≥85% of brief target |
| `aeo_signals`               | 10 | Question headings, lists, FAQ section, ≥2 external citations |

### Modes

| Flag | Behavior |
|---|---|
| `--brief-id N` | Load brief and audit its `target_url` |
| `--url URL` | Override brief's URL (e.g. staging vs production) |
| `--brief-id N --url URL` | Audit a specific URL against a specific brief |
| Only `--url URL` | Generic SEO checks; placement/coverage checks skipped (warn) |
| `--dry-run` | Write report; skip DB inserts |

### Outputs

- **DB:** one `compliance_checks` row per audit (`overall_score`, `issues_json`, `keyword_density`, `meta_status`, `internal_links_count`)
- **Disk:** `outputs/audits/compliance_<slug>_<date>.md` — narrative report grouped by severity, top-5 failure punch list

### Command

```bash
# Score the draft URL captured in the brief
python -m content_operations.compliance_checker --brief-id 1

# Audit staging instead of the brief's target_url
python -m content_operations.compliance_checker --brief-id 1 --url https://staging.damcogroup.com/data-enrichment-services

# Generic check, no brief (just SEO basics)
python -m content_operations.compliance_checker --url https://www.damcogroup.com/ai-agent-development
```

### Verdict thresholds

- **≥85** — "Ready to publish"
- **70–84** — "Revise before publish"
- **<70** — "Major work needed"

---

## 3. Glossary detection

**Module:** `glossary_detector.py` — **Available now.**

Scans every active keyword for definition-intent phrasing, extracts the underlying term, cross-references against existing glossary pages, and produces a prioritized list of missing entries.

### Patterns recognized

| Pattern | Example | Strength |
|---|---|---:|
| `what is X` / `what are X` | "what is agentforce" | 1.0 |
| `X meaning` / `X definition` | "agentforce meaning" | 1.0 |
| `define X` | "define data enrichment" | 1.0 |
| `X explained` | "agentforce explained" | 0.9 |
| `how does X work` | "how does agentforce work" | 0.9 |
| `X basics` / `X fundamentals` | "agentforce basics" | 0.8 |
| `introduction to X` | "introduction to agentforce" | 0.8 |
| `X for beginners` | "agentforce for beginners" | 0.7 |
| `X guide` | "agentforce guide" | 0.6 |

### Priority scoring

Each missing term is scored:
- `strength × ((impressions / 100) + (clicks × 5) + (match_count × 2))`
- Impressions are the strongest demand signal; clicks weight more heavily; multiple matching phrasings (e.g., "what is X" + "X meaning") reinforce the signal.

### Outputs

- `outputs/audits/glossary_gaps_<date>[_<offering>].md` — narrative with priority table + per-term detail
- `outputs/reports/glossary_gaps_<date>.xlsx` — two sheets:
  - Ranked candidates (sortable/filterable)
  - Long-format matching keywords (every kw that triggered a candidate)

### Command

```bash
# Default — all 1,112 active keywords
python -m content_operations.glossary_detector

# One offering
python -m content_operations.glossary_detector --offering "AI"

# Only candidates with real GSC demand
python -m content_operations.glossary_detector --min-impressions 50

# Dry run
python -m content_operations.glossary_detector --dry-run
```

### Cost / time

Free — rule-based, no API calls. Runs in under 1 second across 1,112 keywords.

### Strategic finding from first run (2026-05-28)

**Zero candidates surfaced across all 15 offerings.** Damco's tracked keyword set is 100% commercial intent ("X services", "X company", "X consulting", "X development"). No definitional, educational, or informational searches at all.

This is itself the headline finding for the SEO strategy team:
- AI search engines (Perplexity, ChatGPT search, Google AI Overviews) overwhelmingly cite definitional/educational content. Damco currently has zero SEO surface in that intent category.
- Action: expand keyword research to cover **what is**, **how does**, **X explained**, **X vs Y** variants of Damco's core topics. Even adding ~50-100 such keywords would unlock a meaningful AI-citation opportunity.
- The glossary_detector will then start surfacing real candidates as those keywords are added.

---

## 4. Concentration check

**Module:** `concentration_checker.py` — **Available now.**

Aggregates `content_briefs` over a rolling window (default 90 days) and flags over-concentration across 4 dimensions. Pure rule-based; reads from JSONB so no extra schema needed.

### Dimensions checked

- `offering` — which Damco service line each brief targets
- `audience_stage` — awareness / consideration / decision
- `page_type` — service / pillar / blog / landing / glossary
- `intent` — informational / commercial / transactional

### Flags raised

- Any single bucket >40% of output (default threshold; tunable via `--threshold`)
- Top-two buckets combined >70% (narrow distribution)
- For `offering`: offerings with active keywords but zero briefs in the window

### Output

`outputs/audits/concentration_<date>.md` with:
- Per-dimension distribution tables
- Flagged dimensions + reasons
- Concrete next-step recommendations (e.g. "run brief_generator --coverage-gap --offering 'Microsoft' --limit 5")

### Command

```bash
# Default — 90-day window, 40% threshold
python -m content_operations.concentration_checker

# Tighter window, tighter threshold
python -m content_operations.concentration_checker --days 60 --threshold 30

# One dimension only
python -m content_operations.concentration_checker --dimension offering
```

### When to run

After every `brief_generator` batch — keeps the calendar from quietly skewing.

---

## 5. Query: brief pipeline

**Available now.**

```sql
SELECT cb.id, k.keyword AS target_keyword, p.url, cb.status, cb.assigned_writer,
       cb.date_created
FROM content_briefs cb
LEFT JOIN pages p ON p.id = cb.page_id
LEFT JOIN LATERAL jsonb_array_elements_text(cb.keywords_json) kj ON TRUE
LEFT JOIN keywords k ON k.id = kj::int
ORDER BY cb.date_created DESC LIMIT 20;
```

---

## 6. Query: compliance history

**Available now.**

```sql
SELECT cc.check_date, cc.overall_score, cc.keyword_density, cc.meta_status,
       cc.internal_links_count, cc.issues_json
FROM compliance_checks cc
JOIN pages p ON p.id = cc.page_id
WHERE p.url = %s
ORDER BY cc.check_date DESC LIMIT 10;
```

---

## What to always do

1. Every brief auto-includes the AEO checklist. No exceptions.
2. Every compliance check writes to `compliance_checks` — even if the score is 100, the history matters.
3. Route writing tasks to humans. Never publish content directly.
