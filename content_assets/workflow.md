# Content Assets — Workflow Runbook

Runbook for the Content Assets Agent. **Not yet implemented** — most sections are planning stubs.

## Decision tree

| User says / asks | Section | Status |
|---|---|---|
| "which pillar pages need a PDF/infographic/video" | [1. Asset gap detection](#1-asset-gap-detection) | Planned |
| "draft a whitepaper about X" | [2. Whitepaper drafter](#2-whitepaper-drafter) | Planned |
| "generate a slide deck for X" | [3. Slide generator](#3-slide-generator) | Planned |
| "write a video script for X" | [4. Video script writer](#4-video-script-writer) | Planned |
| "structure an infographic on X" | [5. Infographic structurer](#5-infographic-structurer) | Planned |
| "produce a designed PDF from this draft" | [6. PDF generator](#6-pdf-generator) | Planned |
| "optimize metadata for this asset on [platform]" | [7. Metadata optimizer](#7-metadata-optimizer) | Planned |

---

## 1. Asset gap detection

**Planned module:** `asset_gap_detector.py`

**Behavior when built:** reads `pages` where `page_type = 'pillar'`, cross-references with `content_briefs` entries that produced non-text assets. Flags pillar pages without a downloadable PDF, embedded infographic, or YouTube video. Ranks by MQL-generation potential (traffic × conversion rate).

**Planned command:** `python -m content_assets.asset_gap_detector`

---

## 2. Whitepaper drafter

**Planned module:** `whitepaper_drafter.py`

**Behavior when built:** takes a topic brief (URL, target keywords, audience stage, 3–5 competitor whitepaper references). Uses `CLAUDE_MODEL_COMPLEX` (opus) to draft a 10–20 page whitepaper with:
- Executive summary (1 page)
- Problem statement
- Framework / methodology section
- 2–3 case studies (Damco-provided or placeholders for human to fill)
- Bibliography with real URLs
- CTA

Saves to `outputs/assets/whitepaper/<date>/<slug>.md` — human converts to formatted PDF.

**Planned command:** `python -m content_assets.whitepaper_drafter --brief-id 42`

---

## 3. Slide generator

**Planned module:** `slide_generator.py`

**Behavior when built:** from a brief, generates slide-by-slide content — title, bullet points, speaker notes, image placeholder descriptions. Output is a `.pptx` produced via `python-pptx` with Damco's template + placeholder images.

**Planned command:** `python -m content_assets.slide_generator --brief-id 42 --slides 15`

---

## 4. Video script writer

**Planned module:** `video_script_writer.py`

**Behavior when built:** drafts scripts for 2–5 minute explainer videos. Includes on-screen text cues, B-roll suggestions, voiceover timing. Output is a structured markdown file.

**Planned command:** `python -m content_assets.video_script_writer --brief-id 42 --duration 180`

---

## 5. Infographic structurer

**Planned module:** `infographic_structurer.py`

**Behavior when built:** takes a data-heavy topic, extracts key stats and structures them into an infographic-ready outline (sections, data points, visualization suggestions). Output is JSON that a designer can pick up.

---

## 6. PDF generator

**Planned module:** `pdf_generator.py`

**Behavior when built:** converts a whitepaper markdown draft into a styled PDF using Damco's brand template (header, footer, fonts, colors). Uses `weasyprint` or `reportlab` under the hood.

**Planned command:** `python -m content_assets.pdf_generator --draft outputs/assets/whitepaper/2026-04-17/ai-agent-dev.md`

---

## 7. Metadata optimizer

**Planned module:** `metadata_optimizer.py`

**Behavior when built:** for a given asset + distribution platform, generates platform-tuned metadata:
- SlideShare: title, description, tags, category
- YouTube: title (≤70 chars), description (with timestamps), tags, thumbnail text
- PDF: title, subject, keywords, author
- Infographic directories: title, description, alt text

**Planned command:** `python -m content_assets.metadata_optimizer --asset-path outputs/assets/... --platform youtube`

---

## What to always do

1. Every generated asset is a draft. Flag it as such in the file header.
2. No statistic without a source URL.
3. Use the Damco style guide as a prompt prefix (when it exists).
4. Log generation to `agent_runs` so cost tracking is possible.
