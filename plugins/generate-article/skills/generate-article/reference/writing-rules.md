# Fixed Writing Rules (apply to every article, every channel)

These are the "DO NOT MODIFY" rules from the prompt template. The audit script
checks many of them mechanically, but write to them from the start — fixing after
the fact produces flatter prose.

## Sentence construction
- One idea per sentence. Prefer subject-verb-object.
- If two clauses are joined by "and," "but," or "which," consider two sentences.
- Read each sentence aloud; if you stumble, shorten it.

## Rhythm & human voice (ZeroGPT target: ≤25% AI-detected)
- Vary sentence length deliberately: mix 4-8 word sentences with 15-20 word ones.
  A paragraph where every sentence is the same length reads as machine-generated.
- Let paragraphs be uneven. Some ideas need four sentences, some need two.
- Do not over-resolve every paragraph with a tidy summary sentence. Occasionally
  end on tension and let the reader carry the thought forward.
- Be specific, not general. Replace "organizations see improved efficiency" with a
  concrete, named, numbered consequence. Generalities are the #1 driver of high
  AI-detection scores and of "reads as AI-written" feedback.
- Use grounded, active language over abstract nouns ("cutting the steps your team
  repeats every day," not "optimization of processes").

## Transitions & openers to avoid
Do not start consecutive paragraphs the same way. Avoid these (they read as AI):
- "This is where…" / "This is why…" / "This means…" (pick one, not all)
- "In today's [landscape / world / environment]…" / "In an increasingly…"
- "Let's explore…" / "Let's take a look at…"
- "It's important to note that…" / "It's worth mentioning that…"
- "One of the key…" / "One of the most important…"
- "Ultimately,…" / "Essentially,…" / "Fundamentally,…" as openers
- "By doing so,…" / "As a result,…" more than once per section
- "In conclusion,…" as the literal opening of the close

Instead, open with a fact, a short observation, a direct command, a question, or a
consequence. Mix these.

## Tone rules
- **Affirmative over negative:** state what something is, not what it is not.
- **No contrastive negation:** don't write "This is not a technology problem. It is
  a business problem." Write "This is a business problem."
- **No filler adverbs:** cut extremely, very, definitely, truly, simply.
- **No clichés/buzzwords:** game-changer, cutting-edge, disrupt, synergy, seamless,
  robust, empower, unlock, transform, comprehensive, ecosystem, innovative,
  leverage (verb), scalable (without specifics).
- **No corporate padding:** "In order to," "It is important to note that," "For the
  purpose of," "As mentioned above."

## Headings
- H2/H3 must sound like something a knowledgeable person would say, not a generic
  label. If a heading could appear on any article about any topic, rewrite it.
- **Banned heading patterns:** "Choosing the Right X," "Benefits of X," "What Is X?",
  "Understanding X," "Introduction to X," "The Future of X," "Top N Tips/Ways/Reasons,"
  "Why X Matters," "Conclusion"/"In Conclusion" as a literal heading.
- **But:** descriptive headings must still contain the keyword where it fits — at
  least one H2 carries the primary keyword naturally (SEO feedback requirement).

## Formatting
- Title case for all H2/H3/H4.
- Capitalize proper product/platform names consistently (Salesforce, Lightning
  Experience, Dynamics 365).
- Oxford comma in all lists of three or more.
- Numbers one to nine: spell out. 10 and above: numerals.
- Paragraphs: 100-200 words, three to six sentences. No standalone one-sentence
  paragraphs unless deliberate emphasis.
- **Bullets use a colon, not an em dash, before the description.**
- Vary bullet construction and length — not every bullet starts with a verb, not
  every bullet is the same length.

## Em dashes
- Maximum 2-3 per article. Prefer a comma or two sentences.

## Statistics & linking (NOT relaxed on any channel)
- Use only 2025-2026 stats where available (LinkedIn: 2024-2026). Avoid older data
  unless no recent equivalent exists from a credible source.
- **Primary sources only:** the brand's own published data, IBM, Gartner, McKinsey,
  Forrester, IDC, government bodies, peer-reviewed research. No aggregators, no
  competitor blogs.
- Hyperlink every statistic inline to its source page. Verify the page is live
  before using it (a site that blocks bots but returns current content in search is
  live, not a 404). If a URL can't be confirmed live, drop the stat.
- **Never link to competitor websites.** The brand CTA is the only company page you
  may hyperlink.
- Do not put publication years next to stats in the body. Put years in the Sources
  list only.

## Structure (baseline; adjust section count to content, keep mandatory elements)
1. **Intro (no heading):** open on the business problem or a cited number. Hook in
   the first sentence. 2-3 short paragraphs. Do not introduce yourself or the topic.
2. **Body (H2/H3):** one clear idea per section; the last line of a section should
   make the next feel necessary. Lists for steps/criteria; tables for comparisons.
3. **Conclusion (mandatory, 100-125 words):** summarize the argument in 2-3
   sentences, include the primary keyword and the brand CTA hyperlink, close on a
   forward-looking point (or, on Medium/LinkedIn, a question that invites discussion).
   Do not use "Conclusion" as the literal heading.

## Deliverable (single .docx, produced by md_to_docx.py)
1. SEO metadata block (auto-rendered from the META block).
2. The full article with Word heading styles.
3. Keyword frequency table (auto-computed from `{{KEYWORD_FREQUENCY_TABLE}}`).
4. Sources list: every linked stat with source name, URL, and publication year
   (years here, not in the body).
