# Content Operations — Workflow Runbook

Runbook for the Content Operations Agent. **Not yet implemented** — most sections are planning stubs.

## Decision tree

| User says / asks | Section | Status |
|---|---|---|
| "generate a brief for [keyword/URL]" | [1. Brief generation](#1-brief-generation) | Planned |
| "check this content against the brief", "run compliance" | [2. Compliance check](#2-compliance-check) | Planned |
| "what glossary pages are missing" | [3. Glossary detection](#3-glossary-detection) | Planned |
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

**Planned module:** `glossary_detector.py`

**Behavior when built:** scans `keywords` for patterns like "what is X", "X meaning", "X definition", cross-references against existing glossary pages (`pages.page_type = 'glossary'`), and outputs a prioritized list of missing glossary entries ranked by search volume.

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
