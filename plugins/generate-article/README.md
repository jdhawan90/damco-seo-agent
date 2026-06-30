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

Run once per teammate. Clone the repo (or download it as a ZIP from GitHub), then copy
the skill folder into your user-level skills directory:

**Windows (PowerShell):**
```
git clone https://github.com/jdhawan90/damco-seo-agent.git
Copy-Item -Recurse -Force `
  "damco-seo-agent\plugins\generate-article\skills\generate-article" `
  "$env:USERPROFILE\.claude\skills\generate-article"
```

**macOS / Linux:**
```
git clone https://github.com/jdhawan90/damco-seo-agent.git
mkdir -p ~/.claude/skills
cp -r damco-seo-agent/plugins/generate-article/skills/generate-article ~/.claude/skills/generate-article
```

Restart the Claude Code desktop app. `/generate-article` is now available in every
project you open. To update later: `git pull` and re-run the copy command.

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

## Updating

Maintainer: update the skill, commit, and push. Teammates re-`git pull` and (for
Method A) re-copy, or `/plugin marketplace update damco-tools` for Method C.

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
