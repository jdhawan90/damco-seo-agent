# Sales Enablement — Workflow Runbook

Runbook for the Sales Enablement Agent. **Not yet implemented** — sections below are planning stubs.

## Decision tree

| User says / asks | Section | Status |
|---|---|---|
| "audit this prospect URL", "SEO report for [prospect]" | [1. Prospect audit](#1-prospect-audit) | Planned |
| "generate a client-ready audit report" | [2. Audit report generator](#2-audit-report-generator) | Planned |
| "draft the SEO section of this RFP" | [3. RFP drafter](#3-rfp-drafter) | Planned |
| "show audit history", "list past prospect audits" | [4. Query: audit history](#4-query-audit-history) | Planned |

---

## 1. Prospect audit

**Planned module:** `prospect_auditor.py`

**Behavior when built:**
- Input: prospect domain/URL.
- Runs: site crawl (technical issues), Core Web Vitals (PageSpeed), top 20 keyword SERP positions (DataForSEO), backlink count + DA (DataForSEO), estimated traffic vs. 2 competitors.
- Output: structured JSON audit + a score card (0–100 per dimension).
- Saves to `outputs/audits/<prospect-slug>/<date>/audit.json`.

**Planned command:** `python -m sales_enablement.prospect_auditor --url https://prospect.example.com`

**Cost note:** A single prospect audit uses ~50 DataForSEO queries (SERP + backlinks + keywords) = ~$0.03 on standard queue.

---

## 2. Audit report generator

**Planned module:** `audit_report_generator.py`

**Behavior when built:**
- Takes a prospect audit JSON.
- Uses `CLAUDE_MODEL_DEFAULT` to produce narrative sections (executive summary, opportunities, recommended next steps).
- Renders as a branded PDF using the Damco template.
- Output: `outputs/audits/<prospect-slug>/<date>/audit-report.pdf`.

**Planned command:** `python -m sales_enablement.audit_report_generator --audit-id 42`

**Must-include sections:**
1. Executive summary (1 page)
2. Technical SEO health (CWV, crawlability, schema)
3. Keyword coverage + SERP position samples
4. Backlink profile summary
5. Competitive positioning (vs. 2 competitors)
6. Top 5 recommendations with estimated effort/impact
7. Confidentiality disclaimer (always last page)

---

## 3. RFP drafter

**Planned module:** `rfp_drafter.py`

**Behavior when built:**
- Input: RFP questions (structured or free-form), prospect context.
- Uses `CLAUDE_MODEL_COMPLEX` (opus) for complex multi-section RFPs.
- Output: draft responses per RFP question with Damco case-study references.

**Planned command:** `python -m sales_enablement.rfp_drafter --rfp-file prospect-rfp.docx --prospect "Acme Corp"`

**Safety:** Never promises specific outcomes (e.g., "we will get you to #1 on Google"). Uses conservative language; flags sections that need sales/legal review.

---

## 4. Query: audit history

**Planned** — needs a `prospect_audits` table (migration TBD). Until then, list files under `outputs/audits/`.

---

## Retention policy

All generated files under `outputs/audits/` are subject to a **90-day retention policy** for client data protection. A cleanup cron (to be built) deletes files older than 90 days. The `agent_runs` log entries are retained longer but contain no prospect content — only metadata.

---

## What to always do

1. Flag every draft as "DRAFT — FOR INTERNAL REVIEW BEFORE SENDING".
2. Verify all stats against real data — no LLM hallucination on numbers.
3. Include the confidentiality disclaimer.
4. Clean up old files per the retention policy.
