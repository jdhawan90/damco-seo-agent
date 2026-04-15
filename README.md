# Damco SEO AI Agent System

Production-grade AI agent platform that automates Damco Group's ongoing SEO operations. Implements a 4-phase adoption roadmap: data-liberation agents first, then insight-generating agents, then execution assistants, then cross-agent orchestration.

Derived from the internal `Damco AI Adoption Plan for SEO Operations (v2.1)` and `SEO AI Agent System — Technical Architecture (v1.1)`. The strategy and architecture documents themselves are confidential and not part of this repository.

## What this repo contains today

This is the **foundation layer** described in Architecture doc §9, Week 0. It provides the shared plumbing that every agent will build on:

- **PostgreSQL schema** covering keywords, rankings, pages, briefs, compliance, backlinks, off-page activities, competitors, technical issues, Core Web Vitals, internal links, agent runs, and the cross-agent event bus.
- **Connector modules** for DataForSEO (SERP, keywords, backlinks, on-page), Google Search Console (search analytics, URL inspection), and PageSpeed Insights (Core Web Vitals).
- **Config + database layers** (`common/config.py`, `common/database.py`) with pooled connections, agent run tracking, and strict env-var validation.
- **Migration runner** (`sql/migrate.py`) idempotent, single-command.
- Empty agent-domain folders ready for Phase 1 agents (rank tracker, site auditor, backlink tracker, compliance checker, CWV monitor).

No agents are implemented yet — that's Week 1 onward.

## Architecture at a glance

```
External APIs  ─────────┐
                        ▼
               common/connectors/   ← all network I/O lives here
                        ▼
                common/database.py  ← PostgreSQL (single shared DB)
                        ▼
          Domain agents (keyword_intelligence/, technical_seo/, ...)
                        ▼
         outputs/ + Slack/email notifications + triggers table
```

Design principles (Architecture doc §1):
- Shared database from day one — no point-to-point integrations between agents.
- Standard agent lifecycle: **Read → Process → Write → Notify**.
- Rule-based first, LLM only where it earns its keep (content generation, gap analysis).
- Cron for scheduling (Phase 1–3). Database-backed event bus for cross-agent triggers (Phase 4).
- Minimal infrastructure: a single Linux VM, PostgreSQL on the same host, no containers.

## Prerequisites

- **Python 3.11+**
- **PostgreSQL 14+** running locally (or reachable via `DATABASE_URL`)
- API credentials for DataForSEO, Google Search Console, PageSpeed Insights, and Anthropic (for future LLM-powered agents)

## Setup

```bash
# 1. Clone and enter the repo
git clone https://github.com/<your-org>/damco-seo-agents.git
cd damco-seo-agents

# 2. Python environment
python -m venv .venv
.venv\Scripts\activate          # Windows PowerShell / CMD
# source .venv/bin/activate     # Linux / macOS
pip install -r requirements.txt

# 3. Environment variables
cp .env.example .env
# Edit .env — fill in DATABASE_URL, DataForSEO, PageSpeed, Anthropic keys
# GSC needs a one-time OAuth consent (see below)

# 4. Create the database (first-time only)
psql -U postgres -c "CREATE DATABASE damco_seo;"
psql -U postgres -c "CREATE USER damco_seo WITH PASSWORD 'CHANGE_ME';"
psql -U postgres -c "GRANT ALL PRIVILEGES ON DATABASE damco_seo TO damco_seo;"

# 5. Run migrations
python sql/migrate.py
```

Expected output from the migration runner:

```
  apply  001_initial_schema.sql

Done. 1 migration(s) applied, 0 already in place.
```

Re-running is safe — already-applied migrations are skipped.

### Google Search Console — one-time setup

1. In Google Cloud Console, create OAuth 2.0 client credentials for a **Desktop app**. Download the JSON to `secrets/gsc_client_secrets.json` (create the `secrets/` directory first — it's ignored by git).
2. Make sure your Google account has verified ownership of the site in Search Console and that `GSC_SITE_URL` in `.env` matches the verified property exactly (including the trailing slash).
3. First time you call any `common.connectors.gsc` function, a browser window opens for consent. The resulting refresh token is saved to `secrets/gsc_token.json` — subsequent runs are silent.

## Repository layout

```
damco-seo-agents/
├── common/
│   ├── config.py                  # Env-var loader + typed Settings dataclass
│   ├── database.py                # Connection pool, helpers, agent_runs tracking
│   └── connectors/
│       ├── dataforseo.py          # SERP, keywords, backlinks, on-page audit
│       ├── gsc.py                 # Search Analytics, URL Inspection, sitemaps
│       └── pagespeed.py           # Core Web Vitals + Lighthouse performance score
│
├── sql/
│   ├── 001_initial_schema.sql     # All tables + indexes + triggers
│   └── migrate.py                 # Idempotent migration runner
│
├── keyword_intelligence/          # Phase 1: rank tracker, striking distance, reports
├── competitive_intelligence/      # Phase 2: competitor monitor, backlink analyzer
├── content_operations/            # Phase 2–3: brief generator, compliance checker
├── technical_seo/                 # Phase 1: site auditor, CWV monitor, sitemap validator
├── offpage_links/                 # Phase 1–3: backlink tracker, outreach drafter
├── content_assets/                # Phase 3: whitepaper/slide/video drafters
├── sales_enablement/              # Phase 4: prospect auditor
├── cron/                          # Cron job configs (per-agent)
│
├── outputs/                       # Generated files (runtime, gitignored)
├── requirements.txt
├── .env.example
├── LICENSE                        # MIT
└── README.md
```

## Next build tasks

Per Architecture doc §9:

| Week | Task | Dependency |
|------|------|------------|
| 1–2 | Upgrade existing `rank_tracker.py` (currently at `../damco-rank-tracker/`) to use this database and connector architecture | Foundation |
| 2–3 | Technical site auditor + CWV monitor | Foundation + crawler connector |
| 3–4 | Backlink tracker (dual source) + content compliance checker | Foundation |

## Security

- **No secrets in code.** Every credential comes from `.env` (gitignored).
- Local-only PostgreSQL connections by default.
- `secrets/` directory for OAuth token files — gitignored.
- Generated prospect audit reports are written to `outputs/audits/` with a 90-day retention policy (to be enforced by a cleanup cron job).

## License

[MIT](./LICENSE).
