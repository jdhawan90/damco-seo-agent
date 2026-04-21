# Damco SEO AI Agent System

You are interacting with the **Damco SEO AI Agent System** — a multi-agent platform that automates ongoing SEO operations for Damco Group. The system is built on a shared PostgreSQL database, a connector abstraction for external APIs (DataForSEO, Google Search Console, PageSpeed Insights, Anthropic), and a standard agent lifecycle.

This file orients you to the system when the working directory is the repo root. When the working directory is a specific agent folder (e.g. `keyword_intelligence/`), that folder's `CLAUDE.md` takes precedence.

## Architecture in one paragraph

Every agent follows **Read → Process → Write → Notify**. It pulls data from `common/connectors/*` (never directly from external APIs), applies rule-based logic (or a Claude API call for language tasks), writes to the shared database in `sql/`, and notifies via console/email/Slack. Every run is logged in `agent_runs`. Cron drives scheduling in Phase 1–3; a DB-backed event bus (`triggers` table) activates in Phase 4.

For the full architecture see the `Damco_SEO_AI_Agent_Architecture` document (stored outside the repo — it's confidential).

## Agent directory

| Folder | Domain | Phase | Status |
|---|---|---|---|
| `keyword_intelligence/` | Keyword rank tracking (DataForSEO + GSC dual-lens) | 1–2 | **Active** |
| `technical_seo/` | Site audits, Core Web Vitals, internal linking, sitemap/robots validation | 1 | Planned |
| `offpage_links/` | Backlink tracking, platform discovery, outreach drafting | 1–3 | Planned |
| `content_operations/` | Content briefs, compliance checks, glossary gaps | 2 | Planned |
| `competitive_intelligence/` | Competitor monitoring, backlink profiling, gap analysis | 2 | Planned |
| `content_assets/` | Whitepapers, slide decks, video scripts, infographics, PDFs | 3 | Planned |
| `sales_enablement/` | Prospect SEO audits, RFP drafting | 4 | Planned |

Each agent folder has its own `CLAUDE.md` and `workflow.md`. Use them as the authoritative reference when operating inside that folder.

## Routing — which agent owns what

When the user's request is ambiguous, use this table to decide which sub-agent should handle it (and point them there):

| User intent | Agent |
|---|---|
| "track rankings", "update keyword positions", "GSC data", "striking distance", "report for executives" | `keyword_intelligence/` |
| "site audit", "broken links", "CWV", "core web vitals", "sitemap issues", "canonical problems", "internal links" | `technical_seo/` |
| "backlinks", "outreach", "guest posts", "platform opportunities" | `offpage_links/` |
| "content brief", "compliance check", "glossary coverage", "new page content plan" | `content_operations/` |
| "competitor changes", "competitor backlinks", "gap analysis", "what are they doing we're not" | `competitive_intelligence/` |
| "whitepaper", "infographic", "slide deck", "video script", "downloadable PDF" | `content_assets/` |
| "prospect audit", "sales deck", "RFP response", "client pitch" | `sales_enablement/` |

If the user's intent spans multiple agents, call out the primary owner and mention which other agents will consume the output.

## System-level principles (always apply)

1. **Connector abstraction is sacred.** Agents never call DataForSEO / GSC / PageSpeed / Anthropic directly. Use `common/connectors/*`. If a connector doesn't yet expose what an agent needs, add a method to the connector — don't bypass it.

2. **Shared database, no point-to-point integrations.** Agents communicate through tables in `sql/`, not through direct imports of each other's modules. Cross-agent events go through the `triggers` table in Phase 4.

3. **Rule-based first, LLM second.** Use the Claude API (via `common/llm.py` once it exists) only for genuine language tasks: content drafting, natural-language summaries, ambiguous classification. Don't use it for data aggregation, counting, sorting, or anything a SQL query can do.

4. **Never commit data-import scripts.** One-off spreadsheet loaders belong in an inline script run once and deleted. The repo is for long-lived agent code. Principle is repeated in each agent's `workflow.md` ad-hoc import section.

5. **Never wipe history without explicit user instruction.** `keyword_rankings`, `backlinks`, `agent_runs`, competitor data — all are irreplaceable. Deletion requires the user typing "wipe" or equivalent.

6. **Every run logs to `agent_runs`.** Use `common.database.record_agent_run()`. Don't skip this even for short scripts — operational visibility depends on it.

7. **Secrets in `.env` only.** No credentials in code, no credentials in `CLAUDE.md`/`workflow.md`, no credentials in commit messages. `secrets/` and `.postgres-credentials.txt` are gitignored by design.

8. **Cost awareness.** DataForSEO queries cost money. Default to the standard queue. Confirm with the user before runs that exceed ~$1 (~1,600 queries).

9. **Model tier matters.** Claude API tiers from cheap to expensive:
   - `CLAUDE_MODEL_CHEAP` (default: haiku-4-5) — routine classification, simple extraction
   - `CLAUDE_MODEL_DEFAULT` (default: sonnet-4-6) — most content generation, analysis
   - `CLAUDE_MODEL_COMPLEX` (default: opus-4-6) — deep analysis, multi-step reasoning

## How to respond when invoked at repo root

1. Read this file. Identify which agent the user's request maps to.
2. If the request clearly belongs to one agent, **delegate** — change working directory context to that agent folder, read its `CLAUDE.md` and `workflow.md`, execute the workflow. Tell the user which agent you're acting as.
3. If the request is system-wide (e.g., "run all agents", "what's the last run across the system", "add a new migration"), handle it at this level. See the system-wide commands below.
4. If the request is ambiguous, ask one clarifying question with two or three concrete options drawn from the routing table.

## System-wide commands

| Task | Command |
|---|---|
| Apply pending migrations | `python sql/migrate.py` |
| List all tables | SQL: `SELECT tablename FROM pg_tables WHERE schemaname='public'` |
| Latest 10 agent runs across all agents | SQL: `SELECT agent_name, status, records_processed, run_date FROM agent_runs ORDER BY run_date DESC LIMIT 10` |
| Add a new migration | Create `sql/NNN_description.sql` following the pattern in existing migrations, then run the migrate command |
| List active agents | `ls -d */` in repo root (excludes `common/`, `sql/`, `outputs/`) |

## Safety + verification

- **Never run destructive SQL** (`DROP`, `TRUNCATE`, `DELETE FROM` without WHERE) without explicit user approval.
- **Never modify `common/` from an agent folder.** Those modules are shared. Changes need to be reviewed for impact across all agents.
- **Never write keys, passwords, or tokens** to any file in the repo. `.env`, `secrets/`, and `.postgres-credentials.txt` are the only acceptable locations and they are all gitignored.
- **Always verify reads before claiming success.** "Inserted 200 rows" means nothing without a follow-up `SELECT count(*)`.

## References

- `README.md` — setup instructions and architecture summary (public-facing)
- `.env.example` — full list of configuration variables
- Each agent's `CLAUDE.md` + `workflow.md` — agent-specific runbooks
- Confidential architecture and adoption docs live outside the repo
