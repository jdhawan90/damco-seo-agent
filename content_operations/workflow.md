# Content Operations — Workflow Runbook

Runbook for the Content Operations Agent. All four modules are built. Two have
been validated against production data; two have never run — read
[§0 Before you run anything](#0-before-you-run-anything) first.

## Decision tree

| User says / asks | Section | Status |
|---|---|---|
| "is any of this working", "what's blocked" | [0. Before you run anything](#0-before-you-run-anything) | Available |
| "generate a brief", "create content briefs from gap_analyzer", "what should writers work on" | [1. Brief generation](#1-brief-generation) | **Validated** |
| "check this content against the brief", "run compliance" | [2. Compliance check](#2-compliance-check) | Built, **never run** |
| "what glossary pages are missing", "definition-intent gaps", "AEO opportunities" | [3. Glossary detection](#3-glossary-detection) | **Validated** |
| "are we over-concentrated on X", "content calendar balance" | [4. Concentration check](#4-concentration-check) | Built, **never run** |
| "show open briefs", "brief status" | [5. Query: brief pipeline](#5-query-brief-pipeline) | Available |
| "show compliance history for page X" | [6. Query: compliance history](#6-query-compliance-history) | Available |

---

## 0. Before you run anything

### Prerequisite: migrations

`brief_generator`, `glossary_detector` and `concentration_checker` resolve the
client's identity through `common/tenant.py`, which reads the `tenant*` tables
from migration 012. Without a tenant row, `profile()` raises
`TenantNotConfigured` and the module stops before doing any work — deliberately,
because the alternative is writing briefs in the wrong company's voice.

```bash
python sql/migrate.py
```

Two migrations matter here specifically:

- **012** seeds the tenant, its offerings, and the `glossary_url_path` policy.
- **013** seeds `brief_outline_templates` — the per-stage H2 skeletons `brief_generator` falls back to. Without it the module logs a warning and uses a four-heading generic skeleton.

(`compliance_checker` is the exception: it reads no tenant profile and calls no
model. It needs only the database and a crawlable URL.)

### Check status before promising anyone a number

```bash
python -m common.agents --folder content_operations
```

Joins the agent catalogue to `agent_runs`. Today `brief_generator` and
`glossary_detector` last ran 2026-05-28; `compliance_checker` and
`concentration_checker` read `never`. Treat their first run as a trial.

### What changed today

- **`concentration_checker` had never run because it crashed on startup.** A bare `%` in an argparse help string produced `ValueError: badly formed help string` — argparse runs help text through `%`-formatting, so the literal needed to be `%%`. `--help` works now; verify with it before scheduling anything.
- **`brief_generator` shipped a literal `{primary_kw}`** into awareness-stage briefs from a missing `f` prefix. Outline headings are now stored as plain templates in the database and interpolated uniformly in Python, so the placeholder can no longer leak through any stage. Awareness-stage briefs generated before today contain the literal text — regenerate them.
- **`glossary_detector` read a hardcoded `/glossary/` path.** It now reads the `glossary_url_path` policy. This only ever mattered for a client publishing under `/terms/` or `/wiki/`, who would have seen zero coverage and every term reported as a gap.
- **`brief_generator` passes brand identity via `system=`**, using `system_preamble()`. Its user prompt is tenant-neutral.

---

## 1. Brief generation

**Module:** `brief_generator.py` — validated 2026-05-28.

Takes a target keyword (or set of keywords) and emits a complete SEO content
brief — the document a writer needs to draft a ranking page. Designed to chain
off `competitive_intelligence.gap_analyzer`.

### Modes

| Flag | Behavior |
|---|---|
| `--coverage-gap` | Auto-picks coverage-gap keywords (we're missing from the top 100, ≥1 tracked competitor in the top 10), ranked by GSC impressions. The primary mode. |
| `--keyword-ids 42,43,45` | Manual: brief for these specific keyword IDs |
| `--offering "AI"` | Restrict the coverage-gap pool to one offering |
| `--limit N` | Cap on coverage-gap briefs per run (default: 10) |
| `--no-llm` | Force rule-based output (skip Claude even if available) |
| `--dry-run` | Write the markdown brief to disk but skip DB inserts |
| `--verbose` / `-v` | Debug logging |

### What's in each brief

| Section | How it's built |
|---|---|
| Primary keyword + suggested URL | Slug derived from keyword |
| GSC demand (14d) | clicks, impressions, position from `keyword_rankings` |
| Audience stage (awareness / consideration / decision) | Rule-based heuristic on keyword wording ("what is X" = awareness, "X pricing" = decision) |
| Secondary keywords | Lexical-overlap scoring across all keywords in the same offering, ranked by GSC demand |
| Top 5 competitor reference URLs | From `competitor_rankings` — the SERP we need to outrank |
| Heading outline | Per-stage template from the `brief_outline_templates` tenant policy, refined by the LLM into 6-8 specific H2s |
| Must-include subtopics + buyer questions | LLM-generated from competitor context |
| Internal linking suggestions | Topical match against `pages` (audited via site_auditor). Generic tokens like "services" / "solutions" / "company" are excluded from matching so we don't false-positive on every service page. |
| Narrative angle (intro hook / topic angle / unique POV) | LLM, with `[PLACEHOLDER]` markers when Anthropic credit isn't available |
| **AEO checklist** | Hardcoded — present in every brief regardless of LLM availability |
| Recommended word count | Page-type-aware (service: 1000, pillar: 1500, blog: 800, etc.) |

### Outline templates live in the database (migration 013)

`template_h2_sections()` used to hardcode three heading skeletons, one per
audience stage. They are not a throwaway default — they are the outline that
actually ships whenever LLM enrichment falls back. They were also unmistakably
one company's voice: "Our methodology", "Industries we serve", "Pricing and
engagement models" assume a B2B firm selling delivery services.

They now live in the `brief_outline_templates` tenant policy, keyed by stage
(`awareness`, `consideration`, `decision`), with `consideration` as the fallback
for any unrecognised stage. The seeded values are byte-identical to the old
hardcoded ones, so behaviour did not change.

`{primary_kw}` is the only recognised placeholder, and the **code** does the
substitution — the database stores a plain template, not a format string. That
is also what closed the missing-`f`-prefix bug: interpolation now happens in one
place for every stage instead of per-list.

To retune the outline for a different vertical, edit the policy row. Do not edit
Python.

### LLM behavior

`common.llm.call_claude` with tier `default` (Sonnet). One call per brief
(~2k in / ~1.5k out / ~$0.02-0.05). Brand identity is supplied through
`system=system_preamble("You are writing an SEO content brief.")`; the user
prompt names no company. On `LLMUnavailableError` the narrative sections show
`[PLACEHOLDER — load Anthropic credit and re-run]` markers, the outline falls
back to the template skeleton, and every structured section still populates.

### Outputs

- **DB:** one `content_briefs` row per brief with `status='draft'`, `brief_content` (JSONB), `target_url`, `file_path`, `keywords_json`
- **Disk:** `outputs/briefs/<slug>_<date>.md` — writable markdown brief ready to hand to a writer

### Command

```bash
# Top 10 coverage gaps across all offerings, full LLM enrichment
python -m content_operations.brief_generator --coverage-gap --limit 10

# Top 5 coverage gaps in the AI offering only
python -m content_operations.brief_generator --coverage-gap --offering "AI" --limit 5

# Manual: specific keyword cluster
python -m content_operations.brief_generator --keyword-ids 42,43,45

# Rule-based only (no LLM cost / when credit isn't loaded)
python -m content_operations.brief_generator --coverage-gap --limit 5 --no-llm

# Preview without DB inserts
python -m content_operations.brief_generator --coverage-gap --limit 3 --dry-run
```

### Cost

~$0.02-0.05 per brief with Sonnet. A 10-brief coverage-gap batch costs
~$0.20-0.50. With `--no-llm`, or without Anthropic credit: $0 (rule-based
output, `[PLACEHOLDER]` markers in the narrative sections).

### Validation (2026-05-28)

- Coverage-gap mode picked the right 3 BPM targets including `data enrichment services`.
- Brief for `data enrichment services` showed:
  - GSC demand correctly: 431 impressions, 2 clicks, avg position 29
  - 8 secondary keywords scored by lexical overlap with the primary
  - Top 5 competitors: blackbaud.com, snov.io, zapier.com, alation.com, edq.com (matches the manual SERP check)
  - AEO checklist with 9 items
  - Audience stage = consideration (service-class keyword)
- DB write verified: `content_briefs` row #1 created with `status='draft'`, `file_path` populated, `agent_runs` logged.

This validation predates both the outline-template move and the
`{primary_kw}` fix. Briefs generated on or before that date should be spot-
checked for a literal `{primary_kw}` in awareness-stage outlines.

---

## 2. Compliance check

**Module:** `compliance_checker.py` — built, **never run**.

Crawls a submitted draft URL and scores it against the brief that was generated
for the same target. 12 weighted dimensions, 0-100 score, per-issue
pass/warn/fail. Pure rule-based — it imports no LLM and no tenant profile, so it
carries no API cost and no tenant prerequisite.

Untested against production data. Treat the first run as a trial: check the
crawl actually fetched the page before trusting any dimension that depends on
body text.

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

The weights sum to 100 and the module asserts it. There is no brand-voice or
banned-word dimension — adding one means re-weighting all twelve. (A
`compliance_dimension_weights` policy row exists in migration 012 for future
per-client weighting; the module does not read it yet.)

### Modes

| Flag | Behavior |
|---|---|
| `--brief-id N` | Load the brief and audit its `target_url` |
| `--url URL` | Explicit draft URL; overrides the brief's `target_url` |
| `--brief-id N --url URL` | Audit a specific URL against a specific brief (e.g. staging) |
| `--url URL` alone | Generic SEO checks; placement/coverage checks are skipped with a warn |
| `--dry-run` | Write the report; skip DB inserts |
| `--verbose` / `-v` | Debug logging |

### Outputs

- **DB:** one `compliance_checks` row per audit (`overall_score`, `issues_json`, `keyword_density`, `meta_status`, `internal_links_count`)
- **Disk:** `outputs/audits/compliance_<slug>_<date>.md` — narrative report grouped by severity, with a top-5 failure punch list

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

**Module:** `glossary_detector.py` — validated 2026-05-28.

Scans every active keyword for definition-intent phrasing, extracts the
underlying term, cross-references against existing glossary pages, and produces
a prioritized list of missing entries.

### Where glossary pages live is tenant config

Coverage is determined by matching `pages.page_type = 'glossary'` **or** a URL
containing the tenant's glossary path, which comes from the `glossary_url_path`
policy (default `/glossary/`, normalized to leading and trailing slashes). The
same path drives the slug regex that extracts the covered term.

This was hardcoded. A client publishing under `/terms/` or `/wiki/` would have
matched nothing — zero coverage, and every term they already document reported
as a gap. If a run claims suspiciously total coverage gaps, check the policy
value before believing the report.

The definition-intent patterns below stay in code deliberately: they encode
English grammar, not client identity, and the next client would want the same
ones.

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

`strength × ((impressions / 100) + (clicks × 5) + (match_count × 2))`

Impressions are the strongest demand signal; clicks weight more heavily;
multiple matching phrasings ("what is X" + "X meaning") reinforce the signal.

### Outputs

- `outputs/audits/glossary_gaps_<date>[_<offering>].md` — narrative with priority table + per-term detail
- `outputs/reports/glossary_gaps_<date>.xlsx` — two sheets: ranked candidates, and long-format matching keywords

### Command

```bash
# Default — every active keyword
python -m content_operations.glossary_detector

# One offering
python -m content_operations.glossary_detector --offering "AI"

# Only candidates with real GSC demand
python -m content_operations.glossary_detector --min-impressions 50

# Report only, no agent_runs row
python -m content_operations.glossary_detector --dry-run
```

Flags: `--offering`, `--min-impressions` (default 0), `--dry-run`, `--verbose`.

### Cost / time

Free — rule-based, no API calls. Runs in under a second across ~1,100 keywords.

### Strategic finding from the first run (2026-05-28)

**Zero candidates surfaced across all 15 offerings.** The tracked keyword set is
100% commercial intent ("X services", "X company", "X consulting", "X
development"). No definitional, educational, or informational searches at all.

That is itself the headline finding for the SEO strategy team:

- AI search engines (Perplexity, ChatGPT search, Google AI Overviews) overwhelmingly cite definitional and educational content. We currently have zero SEO surface in that intent category.
- Action: expand keyword research to cover **what is**, **how does**, **X explained**, **X vs Y** variants of the core topics. Even 50-100 such keywords would unlock a meaningful AI-citation opportunity.
- `glossary_detector` will start surfacing real candidates as those keywords are added.

A zero result has two possible causes now — no definition-intent keywords, or a
wrong `glossary_url_path`. The 2026-05-28 finding was the former; the report
lists the keyword-side evidence, so check it rather than assuming.

---

## 4. Concentration check

**Module:** `concentration_checker.py` — built, **never run**.

Aggregates `content_briefs` over a rolling window (default 90 days) and flags
over-concentration across 4 dimensions. Pure rule-based; reads from JSONB, so no
extra schema is needed.

### It could not start until today

`--help` and every other invocation raised `ValueError: badly formed help
string`. Argparse runs help text through `%`-formatting, and one help string
contained a bare `%`; it needed `%%`. That single character is why this module
had never executed despite being listed as built. Confirm it starts before
scheduling it:

```bash
python -m content_operations.concentration_checker --help
```

### Dimensions checked

- `offering` — which service line each brief targets
- `audience_stage` — awareness / consideration / decision
- `page_type` — service / pillar / blog / landing / glossary
- `intent` — informational / commercial / transactional

### Flags raised

- Any single bucket >40% of output (default threshold; tunable via `--threshold`)
- Top-two buckets combined >70% (narrow distribution)
- For `offering`: offerings with active keywords but zero briefs in the window

### Output

`outputs/audits/concentration_<date>.md` with per-dimension distribution tables,
flagged dimensions and reasons, and concrete next-step commands (e.g. "run
brief_generator --coverage-gap --offering 'Microsoft' --limit 5").

### Command

```bash
# Default — 90-day window, 40% threshold
python -m content_operations.concentration_checker

# Tighter window, tighter threshold
python -m content_operations.concentration_checker --days 60 --threshold 30

# One dimension only
python -m content_operations.concentration_checker --dimension offering
```

Flags: `--days` (90), `--threshold` (40.0), `--dimension`
(`offering` | `audience_stage` | `page_type` | `intent`), `--dry-run`,
`--verbose`.

### When to run

After every `brief_generator` batch — it keeps the calendar from quietly
skewing. There are only a handful of briefs in the table so far, so early runs
will flag concentration on a sample too small to mean much. Read the counts, not
just the percentages.

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

Returns nothing until `compliance_checker` runs for the first time.

---

## What to always do

1. Apply migrations before anything else. No tenant row, no run — and without migration 013 the brief outlines fall back to a generic skeleton.
2. Check `python -m common.agents --folder content_operations` before reporting status. Do not read it off a markdown table.
3. Every brief auto-includes the AEO checklist. No exceptions.
4. Every compliance check writes to `compliance_checks` — even a score of 100, because the history matters.
5. Tune client-specific content policy in `tenant_policies` (`brief_outline_templates`, `glossary_url_path`), never in Python.
6. Route writing tasks to humans. Never publish content directly.
