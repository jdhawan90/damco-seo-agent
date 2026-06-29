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

## Install (each teammate runs once)

```
/plugin marketplace add jdhawan90/damco-seo-agent
/plugin install generate-article@damco-tools
```

`jdhawan90/damco-seo-agent` is the GitHub repo hosting this marketplace, and
`damco-tools` is the marketplace name (from `.claude-plugin/marketplace.json`). For a
non-GitHub git host, use the full clone URL instead of the `owner/repo` shorthand.

## Use

```
/generate-article:generate-article
```

Then give it: platform + title + primary keyword (everything else is optional).

## Updating

Maintainer: bump the `version` in `.claude-plugin/marketplace.json` and in
`plugins/generate-article/.claude-plugin/plugin.json`, commit, and push.

Teammates pick up updates with:
```
/plugin marketplace update damco-tools
```

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
