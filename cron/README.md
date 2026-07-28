# Scheduling

Nothing in this system was scheduled until now. Every row in `agent_runs` was
launched by hand, which is why six agents were overdue against their own
declared cadence and the `technical_seo` data was eleven weeks old when the
dashboard work started.

An agent with a `cadence_days` in the registry and no scheduler behind it is a
documentation claim, not a behaviour.

## Install

**Linux** (the deployment target described in the README):

```bash
crontab cron/crontab      # replaces the current user's crontab
crontab -l                # verify
```

Edit `REPO` and `PY` at the top of `cron/crontab` if the repo is not at
`/opt/damco-seo-agents`.

**Windows** (workstation or Windows server), from an elevated shell:

```powershell
.\cron\register_windows_tasks.ps1 -WhatIf     # preview, changes nothing
.\cron\register_windows_tasks.ps1             # register
schtasks /Query /TN "\DamcoSEO\" /FO LIST     # verify
.\cron\register_windows_tasks.ps1 -Remove     # tear down
```

The script refuses to register against the Microsoft Store `python.exe` alias
stub, which exists as a file but fails on every invocation. If it stops with a
message about `\WindowsApps\`, create the venv or pass `-Python` explicitly.

## Verify from the agents' point of view

Don't trust either file — ask the system:

```bash
python -m common.agents
```

That joins the agent catalogue to `agent_runs` and flags anything past twice its
cadence as `overdue`. A schedule that installed cleanly but runs a broken
interpreter looks identical to no schedule at all until you check this.

## What runs when, and why

| Cadence | Agents | Cost |
|---|---|---|
| Daily | `trend_scout` | ~$0.05/run |
| Weekly | `competitor_monitor`, `content_monitor`, `event_digest`, `cwv_monitor` | free |
| 1st + 15th | `rank_tracker`, `gap_analyzer` | **~$9.90 per full `rank_tracker` run** |
| 2nd + 16th | `sitemap_validator`, `site_auditor`, `internal_link_analyzer` | free |
| Monthly | `glossary_detector`, `concentration_checker` | free |

`rank_tracker` is the only meaningful cost: ~$0.00465 per keyword on the
standard queue, ~2,126 keywords, so roughly **$20/month** at this cadence. It
only queries keywords whose last snapshot is older than
`keywords.snapshot_frequency_days`, so an extra run mid-cycle is nearly free.

## Ordering

Three pairs where one agent consumes another's output inside the same window:

```
rank_tracker       ->  gap_analyzer, event_digest    (SERP data + events)
sitemap_validator  ->  site_auditor                  (populates `pages`)
backlink_analyzer  ->  platform_finder               (competitor_backlinks)
```

The schedules leave a gap after each producer rather than chaining them. A
producer failure therefore leaves the consumer running on the previous cycle's
data instead of not running at all — the right trade for a nightly batch, and
both outcomes are visible in `agent_runs`.

## Deliberately not scheduled

| Agent | Why |
|---|---|
| `reports` | A renderer. Run it when someone wants a workbook. |
| `brief_generator` | A human chooses which keywords get briefs. |
| `compliance_checker` | Needs a submitted draft URL as input. |
| `outreach_drafter`, `guest_post_drafter` | Drafts are human-gated by design; nothing here ever sends or publishes. |
| `backlink_analyzer`, `backlink_tracker`, `platform_finder` | Blocked on the DataForSEO Backlinks subscription. Commented out in `crontab`, ready to enable. |
| `vendor_scorer` | Nothing to score until outreach activity exists. |

## Logs

Each job appends to `logs/<agent>.log`. `logs/` is gitignored. On Linux add a
`logrotate` entry; they grow slowly but they do grow.

The authoritative record is `agent_runs`, not these files — the logs are for
reading a traceback after the fact.
