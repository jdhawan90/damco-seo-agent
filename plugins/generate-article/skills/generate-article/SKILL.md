---
name: generate-article
description: Generate a publication-ready Damco Solutions article (.docx) from a keyword, title, and platform. Encodes the per-channel prompt rules (SEO Articles, Paid Guest Blog, Guest Blog, Medium, LinkedIn) and a hard SEO compliance gate built from the SEO team's feedback. Researches and inline-cites real 2025-2026 primary-source statistics, then self-audits before shipping. Use when the user wants to write, generate, or draft a blog/article/guest post for any of these channels.
---

# Generate Article (Damco content engine)

You produce one publication-ready `.docx` per run, for a single keyword + title +
platform. The whole point of this skill is to fix the two failure modes the SEO team
flagged, while keeping the writing-quality and human-voice bar from the original
prompts.

## The two failures this skill exists to prevent

1. **Thesis essay that abandons search intent.** The old prompts said "lead with the
   thesis, do NOT write a neutral keyword overview." Claude obeyed too literally and
   shipped a tight argument with almost no statistics, no use-case/benefit/compliance/
   challenge/future coverage, the keyword in zero H2s, and an empty keyword table.
   **Rule here: lead with the thesis, THEN cover the whole intent.** Both, not either.

2. **Listicle with no real entities.** The "Top 17" article buried partners in numbered
   bullets, used placeholder categories, and put keywords at 0. See `listicle-rules.md`.

## Inputs

Collect from the user (ask only for what's missing):
- **Primary keywords** — one or more, comma-separated. Each must appear in the title,
  the first two paragraphs, the conclusion, and at least one carries into an H2.
- **Secondary keywords** — one or more, comma-separated, for semantic coverage across
  the body. They do not need to appear in headings.
- **Title** — the blog title.
- **Platform** — one of: `SEO Articles`, `Paid Guest Blog`, `Guest Blog`, `Medium`,
  `LinkedIn`.
- **Direction / brief** — a one-line note from the team on why this title was chosen
  and the angle they want. This is the steering input. Use it to shape the thesis, the
  angle, and which subtopics to emphasize in the intent map. When the brief and the
  raw keyword pull in different directions, the brief wins on angle; the keyword still
  governs discoverability placement.
- **Thesis / POV** — the one-line argument. If not given, derive it from the brief and
  the title, then confirm with the user before writing.
- **CTA URL** — the Damco service page to link. If unknown, check the known URLs in
  `channel-profiles.md`; if still unsure, ask. Never invent a slug.
- **Reference article URL** — optional.

When multiple primary keywords are given, lead with the first (most important) one in
the title and opening, and distribute the others naturally across H2s and the body. Do
not force every primary keyword into the title if it reads unnaturally; prioritize, and
make sure each still appears in at least one heading or the opening.

## Workflow

### Step 0 — Load the rules
Read `reference/damco-style-guide.md` (the Damco Writing Style Guide — applies to every
channel), `reference/channel-profiles.md` (the row for this platform),
`reference/writing-rules.md` (applies to all channels), and — if the title is a
listicle ("Top N", "Best", "N Companies/Tools/Partners") — `reference/listicle-rules.md`.

House rules now in force: **no FAQ section on any channel**; a **"Key Takeaways"**
section (clear heading) goes immediately after the title on every channel **except
LinkedIn**; LinkedIn / Medium / guest blogs use a **thought-leadership tone and flow**
(confident first-person/we-voice, opinion backed by evidence, a narrative that builds).

### Step 1 — Build the search-intent map (this is what was missing)
Start from the **direction/brief**: it tells you why this title was chosen and the angle
the team wants. Let it set the thesis and decide which subtopics to lead with and which
to keep brief. Then list the subtopics a searcher for this keyword expects. For a typical
"Role of X" or service topic that includes: what X is (a definition callout), real use
cases, benefits/outcomes, the development/implementation approach, the technologies
involved, challenges, **compliance and security where relevant (e.g. HIPAA, GDPR for
healthcare)**, current 2025-2026 trends (e.g. generative AI), and future adoption.
Every item on this map becomes a section or is deliberately folded into one. Missing
obvious subtopics is the #1 reason a draft is rejected. The thesis is the *spine* that
connects these sections — not a replacement for them.

### Step 2 — Research real statistics and EEAT signals
Use WebSearch / WebFetch to find **5+ statistics from 2025-2026** (LinkedIn: 2024-2026)
from primary sources only: the brand's own data, IBM, Gartner, McKinsey, Forrester,
IDC, government bodies, peer-reviewed research. For each:
- Capture the exact figure, the source name, and the live URL. Verify the page loads.
- Note the publication year (for the Sources list — never in the body).
Also gather EEAT material: named organizations/deployments, real implementation
examples, expert positions. If you cannot verify a named outcome, frame it as a clearly
representative scenario — never invent a company, quote, or number.

### Step 3 — Write the article as Markdown
Write to a working file in the scratchpad, e.g. `<scratchpad>/<slug>.md`, using the
contract in `scripts/md_to_docx.py`. Start the file with the `<!--META ... -->` block
(title, meta_title, meta_description [150-160 chars], platform, primary_keywords,
secondary_keywords, cta_url, and `brief` with the team's direction for traceability).
Then:
- `## Key Takeaways` (clear heading) immediately after the title, then a `> ` box of
  3-6 liftable bullets — on every channel **except LinkedIn**, which omits it entirely.
- Intro (no heading): business problem + thesis, objective tone, keyword in first two paras.
- `> ` definition callout for the central term where it helps.
- Body H2/H3 covering the Step-1 intent map; **at least one H2 contains the primary
  keyword**. Inline-cite every stat as `[claim text](https://source-url)`. **No FAQ
  section** — fold those questions into the prose.
- Comparison table(s) where they earn their place; numbered lists for processes.
- Conclusion (100-125 words, no "Conclusion" heading): primary keyword + CTA link +
  forward-looking close (or a discussion question on Medium/LinkedIn).
- `## Sources` list: source name + URL + publication year for every cited stat.
- `{{KEYWORD_FREQUENCY_TABLE}}` on its own line under a `## Keyword Frequency Table`
  heading (counts auto-compute — do not hand-write counts).
Embed the brand CTA URL 2-3 times. Keep the whole piece 2000-2500 words.

### Step 4 — Audit (mechanical gate)
```
python .claude/skills/generate-article/scripts/audit.py <scratchpad>/<slug>.md
```
Fix every `[FAIL]`, then re-run until it passes. Review `[WARN]`s and resolve the ones
that apply. The audit checks: meta description, keyword usage + keyword-in-H2, stat
count, sources section, CTA, Key Takeaways present (non-LinkedIn) / absent (LinkedIn),
no FAQ section, no "click here" link text, listicle entity-per-heading + no placeholders,
em dashes, banned buzzwords/jargon, banned headings, AI-tell openers, colon-not-emdash
bullets, word count, and the keyword-table placeholder.

### Step 5 — Self-review for voice (audit can't catch this)
Read the draft once for the things the script can't measure: does it read like one
thoughtful practitioner? Is sentence length varied? Are paragraphs uneven? Did you
avoid over-resolving every paragraph? Are claims specific and named, not generic?
Tighten anything that reads as assembled-from-parts.

### Step 6 — Convert to .docx
```
python .claude/skills/generate-article/scripts/md_to_docx.py <scratchpad>/<slug>.md "<out_path>.docx"
```
Default output path: the matching `Generated/<Platform>/` folder, named like the
existing files (`Article2_NN_<Title-slug>.docx`). Confirm the filename with the user if
unsure. The converter renders the SEO metadata block, Word heading styles, tables,
callouts, inline hyperlinks, and the auto-computed keyword frequency table.

### Step 7 — Report
Tell the user: output path, word count, how many stats were cited (with sources), the
audit result, and anything that still needs a human (e.g. a designed infographic for
LinkedIn, or a stat you couldn't verify and therefore dropped).

## Batch mode (generate from an Excel of briefs)

When the user points you at an Excel of article briefs (columns: Domain, Title,
keywords-in-one-cell, Brief/Direction), process it row by row. The sheet has **no
platform and no CTA column** — infer both.

### Step B1 — Read and normalize the sheet
```
python .claude/skills/generate-article/scripts/read_batch.py <batch.xlsx> --manifest
```
This prints normalized rows (domain, inferred platform, title, primary/secondary
keywords parsed from the combined cell, brief, and per-row flags) and writes a
`<batch>_manifest.csv` next to the file. The manifest tracks `status` per row
(pending / needs-review / done / failed) and **is resumable**: re-running skips rows
already marked `done`. Platform is inferred via `reference/domain-map.md`.

### Step B2 — Resolve per-row inputs
For each row to process:
- **Platform:** use the inferred value. If the row is flagged (unknown domain),
  confirm the profile with the user once, or leave it `needs-review`.
- **CTA URL:** infer from the keywords/title using the CTA table in
  `reference/domain-map.md`, then verify the page is live. If you cannot confidently
  match a Damco service page, mark the row `needs-review` rather than guessing a slug.
- **Keywords flagged** "no Primary/Secondary labels": treat the list as primary and
  proceed; note it.

### Step B3 — Generate each row
Run the normal Steps 1-6 for the row (research → write → audit → `.docx`), saving to
`Generated/<Platform>/`. Then update the row in the manifest: set `status` to `done`
(or `failed`), and fill `cta_url`, `output_file`, and any `notes`.

### Step B4 — Chunked and resumable (this is how 100+ rows get done)
A single session cannot hold 100 full generations. Process a **bounded chunk** per run
(e.g. the user says "rows 1-15" or "the next 15 pending"). Each article still gets full
research + the audit gate — never batch-skip quality. When the user says "continue the
batch," re-read the manifest and process the next pending rows. Report a short table at
the end of each chunk: row, title, platform, status, file, anything needing a human.

For a much faster run over many rows, a parallel multi-agent workflow (one agent per
row) is possible but spends far more tokens; only do that if the user explicitly asks.

## Infographics (LinkedIn especially)
LinkedIn requires at least one functional visual (scorecard / process flow / stat band /
comparison) in brand palette (navy #1F3864, teal #2A9D8F). If image tooling is
available, generate a clean PNG and embed it; otherwise render the same content as a
formatted comparison table and flag in Step 7 that a designed visual should replace it.
Functional only — never decorative.

## Non-negotiables (from SEO + style-guide feedback)
- Follow the Damco Writing Style Guide on every channel (`reference/damco-style-guide.md`).
- Meta description on every article.
- Primary keyword in the title AND at least one H2.
- 5+ inline-cited primary-source stats (3+ on non-LinkedIn channels), each linked at
  the point of use, year only in Sources.
- Full search-intent coverage including compliance/security and current trends where
  relevant — the thesis organizes the coverage, it does not replace it.
- **No FAQ section on any channel.** Fold those questions into the prose.
- **Key Takeaways** (clear heading, right after the title) on every channel except
  LinkedIn; LinkedIn omits it.
- LinkedIn / Medium / guest blogs read as thought leadership: confident, narrative,
  opinion backed by evidence — no generic filler.
- Listicles: one heading per real, named entity; no placeholders; objective intro.
- Keyword frequency table auto-computed, never estimated.
