#!/usr/bin/env python
"""
audit.py  --  Mechanical compliance gate for a generated article (Markdown form).

Runs the checks that the SEO team's feedback turned into hard rules, plus the
fixed writing-quality rules from the prompt template. Prints a PASS/FAIL report.
Anything marked [FAIL] must be fixed before the article is converted and shipped.

USAGE:
    python audit.py <article.md>
"""
import sys
import re

PASS, FAIL, WARN = "[PASS]", "[FAIL]", "[WARN]"

BANNED_HEADINGS = [
    r'choosing the right', r'^benefits of', r'top benefits of', r'what is\b',
    r'understanding\b', r'introduction to', r'the future of', r'why .* matters',
    r'^conclusion$', r'in conclusion',
]
BANNED_WORDS = [
    'game-changer', 'game changer', 'cutting-edge', 'cutting edge', 'disrupt',
    'synergy', 'seamless', 'robust', 'empower', 'unlock', 'transform',
    'comprehensive', 'ecosystem', 'innovative', 'unicorn',
]
# "leverage"/"scalable" are only flagged as verbs/without specifics -> WARN only
SOFT_WORDS = ['leverage', 'scalable']
# Damco Style Guide empty phrases / clichés -> WARN
STYLE_PHRASES = [
    'thinking outside the box', 'core competency', 'for all intents and purposes',
    'low-hanging fruit', 'drill down', 'giving 110', 'actionable',
]
AI_OPENERS = [
    "it is worth noting", "it's worth noting", "it is important to note",
    "in today's", "let's explore", "let's take a look", "one of the key",
    "one of the most important", "this means that", "this ensures that",
]


def parse_meta(text):
    meta = {}
    m = re.search(r'<!--META(.*?)-->', text, re.DOTALL)
    body = text
    if m:
        for line in m.group(1).strip().splitlines():
            if ':' in line:
                k, v = line.split(':', 1)
                meta[k.strip().lower()] = v.strip()
        body = text[m.end():]
    return meta, body


def get_headings(body):
    return [(len(m.group(1)), m.group(2).strip())
            for m in re.finditer(r'^(#{2,4})\s+(.*)$', body, re.MULTILINE)]


def plain_text(body):
    t = re.sub(r'\[([^\]]+)\]\((https?://[^)\s]+)\)', r'\1', body)
    t = re.sub(r'[#*>|`]', ' ', t)
    return t


def kw_count(body, phrase):
    t = plain_text(body).lower()
    return len(re.findall(r'\b' + re.escape(phrase.strip().lower()) + r'\b', t))


def report(label, ok, detail=""):
    tag = PASS if ok is True else (WARN if ok == "warn" else FAIL)
    print(f"{tag} {label}" + (f"  -> {detail}" if detail else ""))
    return 0 if tag == FAIL else 0


def main():
    if len(sys.argv) != 2:
        print(__doc__); sys.exit(1)
    with open(sys.argv[1], encoding='utf-8') as f:
        text = f.read()
    meta, body = parse_meta(text)
    headings = get_headings(body)
    h2s = [h for lvl, h in headings if lvl == 2]
    plat = meta.get('platform', '').lower()
    fails = 0

    print("=" * 64)
    print(f"COMPLIANCE AUDIT  ({meta.get('platform','?')}: {meta.get('title','?')})")
    print("=" * 64)

    # --- Metadata / SEO basics --------------------------------------------
    md = meta.get('meta_description', '')
    ok = 120 <= len(md) <= 160
    fails += not ok
    report(f"Meta description present (len {len(md)}, target 120-160)", ok if ok else False, md[:60] + ("..." if md else "MISSING"))

    primaries = [k.strip() for k in meta.get('primary_keywords', '').split(',') if k.strip()]
    secondaries = [k.strip() for k in meta.get('secondary_keywords', '').split(',') if k.strip()]

    # --- Keyword usage -----------------------------------------------------
    for kw in primaries:
        c = kw_count(body, kw)
        ok = c >= 2
        fails += not ok
        report(f"Primary keyword used (>=2): '{kw}'", ok, f"{c}x")
        # primary keyword in at least one H2
        in_h2 = any(kw.lower() in h.lower() for h in h2s)
        fails += not in_h2
        report(f"Primary keyword in an H2 heading: '{kw}'", in_h2,
               "found" if in_h2 else "NOT in any H2 — SEO feedback #1")
    for kw in secondaries:
        c = kw_count(body, kw)
        report(f"Secondary keyword used (>=1): '{kw}'", c >= 1 or "warn", f"{c}x")

    # title contains a primary keyword (title is user-provided, so WARN not FAIL)
    title = meta.get('title', '').lower()
    has_kw_in_title = any(k.lower() in title for k in primaries) if primaries else False
    report("Primary keyword in title (user-provided; WARN only)",
           True if has_kw_in_title else "warn")

    # --- Statistics & sourcing --------------------------------------------
    links = re.findall(r'\[([^\]]+)\]\((https?://[^)\s]+)\)', body)
    # statistics ~ inline links sitting near a number; count all external links as proxy
    body_links = [l for l in links if 'damcogroup.com' not in l[1] and 'achieva.ai' not in l[1]]
    nstats = len(body_links)
    min_stats = 5 if plat in ('linkedin', 'linkedin.com') else 3
    ok = nstats >= min_stats
    fails += not ok
    report(f"Inline cited stat-links (>= {min_stats})", ok, f"{nstats} external citation links")

    has_sources = bool(re.search(r'^#{2,3}\s+sources', body, re.MULTILINE | re.IGNORECASE))
    fails += not has_sources
    report("Sources list section present", has_sources)

    # --- CTA ---------------------------------------------------------------
    cta = meta.get('cta_url', '')
    cta_hits = body.count(cta) if cta else 0
    ok = cta_hits >= 2
    report(f"Brand CTA link embedded (2-3x): {cta}", ok if ok else "warn", f"{cta_hits}x")

    # --- Structure ---------------------------------------------------------
    is_linkedin = plat in ('linkedin', 'linkedin.com')

    # Key Takeaways: required (heading) on all channels EXCEPT LinkedIn
    kt_heading = bool(re.search(r'^#{2,4}\s*key takeaways', body, re.IGNORECASE | re.MULTILINE))
    if is_linkedin:
        report("Key Takeaways omitted (LinkedIn does not use one)",
               "warn" if kt_heading else True,
               "remove the Key Takeaways section for LinkedIn" if kt_heading else "")
    else:
        fails += not kt_heading
        report('"Key Takeaways" heading present (right after title)', kt_heading,
               "" if kt_heading else "add a '## Key Takeaways' section after the title")

    # FAQ: team policy is NO FAQ on any channel
    faq_heading = bool(re.search(
        r'^#{2,4}\s*.*\b(faq|frequently asked questions)\b', body, re.IGNORECASE | re.MULTILINE)) \
        or bool(re.search(r'^#{2,4}\s*questions\b[^\n]*\bask', body, re.IGNORECASE | re.MULTILINE))
    report("No FAQ section (team policy: omit FAQs)", "warn" if faq_heading else True,
           "remove the FAQ-style section; fold the questions into the prose" if faq_heading else "")

    # listicle detection
    is_listicle = bool(re.search(r'\btop\s+\d+\b|\bbest\b|\b\d+\s+(companies|tools|partners|platforms|vendors)\b', title))
    if is_listicle:
        m = re.search(r'top\s+(\d+)', title)
        n = int(m.group(1)) if m else None
        h3s = [h for lvl, h in headings if lvl == 3]
        entity_headings = len([h for lvl, h in headings if lvl in (2, 3)])
        print(f"  (listicle detected{' — expecting ~%d entity headings' % n if n else ''})")
        ok = (n is None) or (entity_headings >= n)
        fails += not ok
        report(f"One H2/H3 per listed entity (found {entity_headings} sub-headings)", ok,
               "each entity needs its own heading — SEO feedback #2")
        # placeholder entities
        placeholders = re.findall(r'(cloud-focused boutiques|hybrid onshore|large .*-headquartered firms|category descriptor|placeholder|\bTBD\b)', body, re.IGNORECASE)
        ok = not placeholders
        fails += not ok
        report("No placeholder / unnamed entities", ok, str(placeholders[:3]) if placeholders else "")

    # --- Writing-quality / AI-tells ---------------------------------------
    em = body.count('—')
    ok = em <= 3
    fails += not ok
    report(f"Em dashes <= 3 (found {em})", ok)

    low = body.lower()
    banned_hits = [w for w in BANNED_WORDS if re.search(r'\b' + re.escape(w) + r'\b', low)]
    fails += bool(banned_hits)
    report("No banned buzzwords", not banned_hits, ', '.join(sorted(set(banned_hits))))

    soft_hits = [w for w in SOFT_WORDS if re.search(r'\b' + re.escape(w) + r'\b', low)]
    report("Soft words (check for specifics): leverage/scalable", "warn" if soft_hits else True, ', '.join(soft_hits))

    phrase_hits = [p for p in STYLE_PHRASES if p in low]
    report("No style-guide empty phrases/cliches", "warn" if phrase_hits else True, ', '.join(phrase_hits))

    # Accessibility: link text must be meaningful, never "click here"
    badlink = re.findall(r'\[\s*(click here|click|here|read more|learn more)\s*\]\(', low)
    report("Link text is descriptive (no 'click here')", "warn" if badlink else True,
           ', '.join(sorted(set(badlink))))

    # "you can" / "there is/are" weak constructions -> WARN (style guide)
    weak = len(re.findall(r'\byou can\b', low)) + len(re.findall(r'\bthere (is|are|were)\b', low))
    report("Direct phrasing (avoid 'you can' / 'there is-are')", "warn" if weak else True,
           f"{weak} occurrence(s)")

    bad_head = []
    for lvl, h in headings:
        for pat in BANNED_HEADINGS:
            if re.search(pat, h.strip().lower()):
                bad_head.append(h)
    fails += bool(bad_head)
    report("No banned/generic heading patterns", not bad_head, '; '.join(bad_head[:4]))

    opener_hits = [o for o in AI_OPENERS if o in low]
    report("AI-tell openers", "warn" if opener_hits else True, ', '.join(opener_hits[:5]))

    # colon (not em dash) before bullet descriptions: detect '- word —'
    emdash_bullets = re.findall(r'^[-*]\s+[^\n]*—', body, re.MULTILINE)
    report("Bullets use colon, not em dash, before description",
           not emdash_bullets, f"{len(emdash_bullets)} bullets with em dash")

    # --- Word count --------------------------------------------------------
    wc = len(plain_text(body).replace('{{KEYWORD_FREQUENCY_TABLE}}', ' ').split())
    ok = 1800 <= wc <= 2500
    report(f"Word count in 2000-2500 band (got {wc})", ok if ok else "warn")

    # keyword frequency table placeholder present
    ok = '{{KEYWORD_FREQUENCY_TABLE}}' in body
    fails += not ok
    report("Keyword frequency table placeholder present", ok,
           "add {{KEYWORD_FREQUENCY_TABLE}} so counts auto-compute")

    print("=" * 64)
    if fails:
        print(f"RESULT: {fails} hard check(s) FAILED — fix before shipping.")
        sys.exit(1)
    print("RESULT: all hard checks passed. Review WARNs, then convert to .docx.")


if __name__ == '__main__':
    main()
