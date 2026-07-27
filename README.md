# Damco SEO AI Agent System

A multi-agent platform that automates ongoing SEO operations: rank tracking, technical auditing, competitor monitoring, content briefing, and off-page outreach. Agents share one PostgreSQL database, reach external APIs only through `common/connectors/`, and log every run to `agent_runs`.

Derived from the internal `Damco AI Adoption Plan for SEO Operations (v2.1)` and `SEO AI Agent System — Technical Architecture (v1.1)`. Those documents are confidential and not part of this repository.

## What's here

**22 agents across five domains.** Ask the system itself rather than trusting a table in a README:

```bash
python -m common.agents              # inventory + last-run status per agent
python -m common.agents --validate   # catalogue vs filesystem (CI check)
python -m common.agents --json       # machine-readable
```

| Folder | Agents | State |
|---|---:|---|
| `keyword_intelligence/` | 4 | Rank tracking (DataForSEO + GSC), reports, trend discovery |
| `competitive_intelligence/` | 5 | Competitor pages, content, backlinks, gaps, event digest |
| `technical_seo/` | 4 | Sitemap validation, on-page audit, Core Web Vitals, internal links |
| `content_operations/` | 4 | Briefs, compliance, glossary gaps, calendar concentration |
| `offpage_links/` | 5 | Backlinks, platform discovery, outreach and guest-post drafting, vendor scoring |
| `content_assets/` | 0 | Not started |
| `sales_enablement/` | 0 | Not started |

Three off-page agents are blocked on a DataForSEO Backlinks subscription; `--validate` and the inventory both report that.

## Two ideas the codebase is built around

**No agent knows whose site it is.** Brand name, owned domains, service lines, market, vocabularies and tuned thresholds live in the `tenant*` tables and reach agents through `common/tenant.py`. No agent source file contains a domain, an offering name, a company name, or a client-tuned threshold. Pointing the system at a different company is a database change, not a code change.

Brand identity enters a model call only through `system_preamble()`, passed as `system=` — never interpolated into a user prompt, because a prompt that names the company cannot be fixed by configuration.

**Deterministic is a first-class status, not a gap.** 16 of the 22 agents never call a model and should not. Comparing a page-speed score to a threshold is arithmetic; an LLM would be slower, cost money, and give a different answer each run.

That matters most in `technical_seo/`, where four agents open, update and auto-resolve rows in `technical_issues`. Correctness depends on the same input producing the same issue set every run. A non-deterministic detector would make issues flap open and closed between cycles and corrupt the `date_resolved` history the tables exist for. **Generated text goes to advisory surfaces or cached columns — never into a detector path.** The schema enforces the split: an agent registered as `deterministic` may not declare an LLM tier.

## Architecture

```
External APIs  ─────────┐
                        ▼
               common/connectors/   ← all network I/O lives here
                        ▼
          common/tenant.py          ← who this deployment runs for
          common/database.py        ← PostgreSQL (single shared DB)
          common/llm.py             ← the only Claude entry point
                        ▼
          Domain agents (keyword_intelligence/, technical_seo/, ...)
                        ▼
         outputs/ + console/Slack/email + triggers table
```

- Shared database from day one — no point-to-point integrations between agents.
- Standard lifecycle: **Read → Process → Write → Notify**.
- Rule-based first, LLM only where there is genuine language judgment to make.
- Cron for scheduling (Phase 1–3); a DB-backed event bus (`triggers`) in Phase 4.
- Single Linux VM, PostgreSQL on the same host, no containers.

## Prerequisites

- **Python 3.11+**
- **PostgreSQL 14+** reachable via `DATABASE_URL`
- DataForSEO credentials (required). Google Search Console, PageSpeed, Anthropic and Voyage are all optional — the agents that use them degrade rather than crash.

## Setup

```bash
# 1. Clone and enter the repo
git clone https://github.com/jdhawan90/damco-seo-agent.git
cd damco-seo-agent

# 2. Python environment
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # Linux / macOS
pip install -r requirements.txt

# 3. Environment variables
cp .env.example .env
# Edit .env — DATABASE_URL and DataForSEO are the only required values.

# 4. Create the database (first-time only)
psql -U postgres -c "CREATE DATABASE damco_seo;"
psql -U postgres -c "CREATE USER damco_seo WITH PASSWORD 'CHANGE_ME';"
psql -U postgres -c "GRANT ALL PRIVILEGES ON DATABASE damco_seo TO damco_seo;"

# 5. Run migrations — REQUIRED before any agent will start
python sql/migrate.py

# 6. Confirm the tenant profile loaded
python -m common.agents
```

Re-running migrations is safe; applied ones are skipped.

### The tenant profile is a hard prerequisite

Migration 012 creates the `tenant*` tables and seeds one row. **Until migrations are applied, every agent raises `TenantNotConfigured` on startup.** That failure is deliberate and loud — a silent default would mean an agent crawling, scoring and writing results for the wrong company.

One client per database. `TENANT_SLUG` in `.env` only matters if a database ever holds more than one; leave it unset otherwise.

To retarget the system at a different client, edit the data — no code changes:

| Table | Holds |
|---|---|
| `tenants` | Brand name, primary domain, vertical, audience, market, crawler identity |
| `tenant_domains` | Owned domains and their sitemap URLs (NULL triggers auto-discovery) |
| `tenant_offerings` | Service lines and the token vocabularies that identify them in text |
| `tenant_vocabularies` | Commercial tokens, domain blocklists, URL path maps, banned claims |
| `tenant_policies` | CWV thresholds, thin-content floors, inbound-link floors, house style |

### Google Search Console — one-time setup

1. In Google Cloud Console create OAuth 2.0 credentials for a **Desktop app**. Save the JSON to `secrets/gsc_client_secrets.json` (create `secrets/` first — it's gitignored).
2. Verify site ownership in Search Console and make `GSC_SITE_URL` match the property exactly, trailing slash included.
3. The first `common.connectors.gsc` call opens a browser for consent; the refresh token lands in `secrets/gsc_token.json` and later runs are silent.

### Optional keys

| Variable | Enables | Without it |
|---|---|---|
| `ANTHROPIC_API_KEY` | Narratives, classification, drafting | Agents fall back to rule-based output and say so |
| `VOYAGE_API_KEY` | Semantic near-duplicate detection in `trend_scout` | Falls back to token-set matching |
| `PAGESPEED_API_KEY` | `cwv_monitor` | That agent cannot run |
| `LLM_BUDGET_USD` | Per-process Claude spend ceiling (default 5) | Same default applies |

## Repository layout

```
damco-seo-agents/
├── common/
│   ├── tenant.py                  # Client profile — who this deployment serves
│   ├── agents.py                  # Agent registry + --validate + --sync
│   ├── llm.py                     # Claude wrapper: JSON, retry, caching, budget
│   ├── config.py                  # Env loader + typed Settings
│   ├── database.py                # Pool, helpers, agent_runs tracking
│   ├── sitemap.py                 # Sitemap discovery + recursive walking
│   └── connectors/
│       ├── dataforseo.py          # SERP, Keyword Planner, backlinks, on-page
│       ├── gsc.py                 # Search Analytics, URL Inspection
│       ├── pagespeed.py           # Core Web Vitals
│       ├── crawler.py             # Polite HTML fetcher (robots-aware)
│       ├── feeds.py               # RSS / Atom / Reddit / Hacker News
│       └── embeddings.py          # Voyage vectors + cache
│
├── sql/                           # 16 migrations + idempotent runner
├── keyword_intelligence/          # 4 agents
├── competitive_intelligence/      # 5 agents
├── technical_seo/                 # 4 agents
├── content_operations/            # 4 agents
├── offpage_links/                 # 5 agents
├── content_assets/                # not started
├── sales_enablement/              # not started
├── cron/                          # per-agent schedules
│
├── outputs/                       # generated files (gitignored)
├── requirements.txt
├── .env.example
├── CLAUDE.md                      # system-level operating rules
└── LICENSE                        # MIT
```

Each agent folder carries its own `CLAUDE.md` (scope and safety rules) and `workflow.md` (the runbook). Those are authoritative when working inside that folder.

## Safety rules that are not negotiable

- **Never wipe history.** `keyword_rankings`, `competitor_serp_events`, `backlinks`, `agent_runs` and their siblings are irreplaceable. Deletion requires an explicit instruction.
- **Never send outreach or publish content automatically.** Both drafters write files and log a draft; a human sends.
- **Never promote a keyword candidate without a human choosing it.** Discovery costs ~$0.05 a run; tracking costs money per keyword on every run thereafter, forever.
- **Secrets live in `.env` and `secrets/` only.** Both are gitignored by design.
- **Confirm cost before large DataForSEO runs.** Default to the standard queue; check before exceeding ~$1.

## Security

- No credentials in code, commit messages, or documentation.
- Local-only PostgreSQL connections by default.
- The crawler identifies itself with a user agent built from the tenant profile and respects `robots.txt`.
- Generated audit reports land in `outputs/` (gitignored), 90-day retention to be enforced by a cleanup job.

## License

[MIT](./LICENSE).
