# Content Operations — Workflow Runbook

Runbook for the Content Operations Agent. **Not yet implemented** — most sections are planning stubs.

## Decision tree

| User says / asks | Section | Status |
|---|---|---|
| "generate a brief for [keyword/URL]" | [1. Brief generation](#1-brief-generation) | Planned |
| "check this content against the brief", "run compliance" | [2. Compliance check](#2-compliance-check) | Planned |
| "what glossary pages are missing", "definition-intent gaps", "AEO opportunities" | [3. Glossary detection](#3-glossary-detection) | **Available** |
| "are we over-concentrated on X", "content calendar balance" | [4. Concentration check](#4-concentration-check) | Planned |
| "show open briefs", "brief status" | [5. Query: brief pipeline](#5-query-brief-pipeline) | Available |
| "show compliance history for page X" | [6. Query: compliance history](#6-query-compliance-history) | Available |

---

## 1. Brief generation

**Planned module:** `brief_generator.py`

**Behavior when built:** takes a target URL (or a set of keyword IDs) and produces a complete SEO brief: primary + secondary keywords with SV, competitor reference URLs, target audience stage, heading outline, internal linking targets, AEO checklist, word count recommendation. Saves the brief JSON to `content_briefs.brief_content` and writes a formatted .docx to `outputs/briefs/`.

**LLM usage:** `CLAUDE_MODEL_DEFAULT` (sonnet) for narrative sections (intro hook, topic angle, unique POV).

**Planned command:**
```bash
python -m content_operations.brief_generator --keyword-ids 42,43,45 --target-url https://www.damcogroup.com/ai-agent-development
```

**AEO checklist** (always include in every brief):
- Does the content answer one crisp, extractable question in the first 200 words?
- Is there a "key facts" section with stats / definitions that AI search can quote?
- Are there bulleted lists for scannable content?
- Is the author identified with credentials?
- Are sources cited inline?

---

## 2. Compliance check

**Planned module:** `compliance_checker.py`

**Behavior when built:** reads submitted content from a URL or local file, scores against the brief (keyword density per term, meta title length, meta description length, H1 presence + uniqueness, internal link count + target relevance, image alt text coverage, schema presence), writes the scorecard to `compliance_checks`.

**Planned command:**
```bash
python -m content_operations.compliance_checker --page-id 123 --content-url https://draft.damcogroup.com/new-page
```

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

**Planned module:** `concentration_checker.py`

**Behavior when built:** analyzes the distribution of content briefs (by offering, by audience stage, by keyword intent) over a rolling 90-day window. Flags when any one bucket exceeds 40% of total output — prevents SEO blind spots.

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
