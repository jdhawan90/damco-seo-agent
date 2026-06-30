# Damco Claude Code Tools (`damco-tools` marketplace)

Internal Claude Code plugins for the Damco content team.

## Plugins

| Plugin | What it does |
|---|---|
| **generate-article** | Generates a publication-ready Damco article (`.docx`) from a keyword, title, and platform. Encodes the per-channel rules (SEO Articles, Paid Guest Blog, Guest Blog, Medium, LinkedIn), researches and inline-cites real 2025-2026 primary-source statistics, runs a hard SEO compliance gate, and converts to `.docx` with an auto-computed keyword table. |

## One-time prerequisite (every teammate)

The article-to-`.docx` step uses Python and `python-docx`.

- Install Python (the `py` launcher on Windows; `python3` on macOS/Linux).
- Then install the library:
  ```
  py -m pip install -r plugins/generate-article/skills/generate-article/scripts/requirements.txt
  ```
  (macOS/Linux: `python3 -m pip install python-docx`)

Without Python, the skill still researches and writes the article, but the final
`.docx` conversion will fail.

## Install

A skill is just files in a `.claude/skills/` folder that Claude Code auto-discovers.
It does **not** require the `/plugin` command (which is not available in every Claude
Code build, including the desktop app). Pick whichever method fits.

### Method A — Personal skill (recommended; works in every project, per person)

Clone the repo once, then run the install script. **The same script installs and
updates** — re-run it any time to pull the latest and refresh your local copy.

```
git clone https://github.com/jdhawan90/damco-seo-agent.git
```

**Windows (PowerShell):**
```
powershell -ExecutionPolicy Bypass -File damco-seo-agent\plugins\generate-article\install-skill.ps1
```

**macOS / Linux:**
```
bash damco-seo-agent/plugins/generate-article/install-skill.sh
```

The script does three things: `git pull` the latest, copy the skill into your personal
`~/.claude/skills/generate-article`, and check that `python-docx` is installed. Restart
the Claude Code desktop app and `/generate-article` is available in every project.

**To update later:** just run the same install-skill script again. That is the whole
update step.

### Method B — Open this repo (zero copying)

This repo also ships the skill at its top-level `.claude/skills/generate-article/`. If
you clone this repo and open it as your working folder in Claude Code, `/generate-article`
is available with no copy step. `git pull` keeps it current.

### Method C — Plugin marketplace (only if your build has `/plugin`)

```
/plugin marketplace add jdhawan90/damco-seo-agent
/plugin install generate-article@damco-tools
```

## Use

```
/generate-article
```

Then give it: platform + title + primary keyword (everything else is optional).

## Updating (how teammates get your changes)

Skills are pull-based; there is no automatic push. When the maintainer pushes an update:

- **Method A:** re-run `install-skill.ps1` / `install-skill.sh`. It pulls and re-copies
  in one step. (Tip: tell the team "an update is live, re-run the install script.")
- **Method B (opened the repo):** just `git pull`. The skill is read in place, so the
  pull *is* the update — nothing to copy.
- **Method C (`/plugin`):** `/plugin marketplace update damco-tools`.

Maintainer workflow: edit the skill, commit, and push to `main`. Optionally bump the
`version` in `.claude-plugin/marketplace.json` and `.claude-plugin/plugin.json` so
Method C users see a new version.

## Optional: auto-enable org-wide

A workspace admin can push this to managed/team `settings.json` so everyone gets the
plugin without manual install:

```json
{
  "extraKnownMarketplaces": {
    "damco-tools": { "source": { "source": "github", "repo": "jdhawan90/damco-seo-agent" } }
  },
  "enabledPlugins": { "generate-article@damco-tools": true }
}
```
