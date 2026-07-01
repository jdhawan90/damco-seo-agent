#!/usr/bin/env python
"""
read_batch.py  --  Read a batch Excel of article briefs and emit normalized rows
plus a resumable manifest, for the generate-article skill's batch mode.

Expected sheet columns (header names are matched loosely, any order):
  Domain (Name)  |  Title  |  Keywords (primary + secondary in one cell)  |  Brief/Direction

USAGE:
    python read_batch.py <batch.xlsx>            # print normalized rows as JSON
    python read_batch.py <batch.xlsx> --manifest # also (re)create a manifest CSV
                                                  # next to the xlsx (skips existing
                                                  # 'done' rows so a run resumes)

The keyword cell is split into primary vs. secondary. Recognized forms:
  "Primary: a, b   Secondary: c, d"   |   "Primary - a,b; Secondary - c,d"
  plain "a, b, c"  -> all treated as primary (flagged for review)

Platform is inferred from the Domain (no platform column). Unknown domains fall back
to 'Guest Blog' and are flagged. CTA is NOT inferred here — the agent infers and
verifies it per row at generation time (see SKILL.md batch mode).
"""
import sys
import os
import re
import json
import csv

# Domain -> channel profile (see reference/domain-map.md).
# LinkedIn and Medium are special; everything else (article directories, guest sites,
# and "[PUBLISHING PLATFORM TBD]" rows) uses the SEO-depth default (SEO Articles rules),
# per the team's blanket decision for batch runs.
DOMAIN_PROFILE = {
    'linkedin.com': 'LinkedIn',
    'medium.com': 'Medium',
    'damcogroup.com': 'SEO Articles',
}
DEFAULT_PROFILE = 'SEO Articles'

HEADER_ALIASES = {
    'domain': ['domain name', 'domain', 'website', 'site', 'publishing platform', 'publication'],
    'title': ['blog title', 'article title', 'title', 'topic'],
    'keywords': ['primary and secondary', 'primary/secondary', 'keywords', 'keyword'],
    'brief': ['brief/direction', 'brief / direction', 'direction', 'brief', 'angle', 'pov', 'notes'],
}


def norm_domain(value):
    d = (value or '').strip().lower()
    d = re.sub(r'^https?://', '', d)
    d = re.sub(r'^www\.', '', d)
    d = d.split('/')[0].strip()
    return d


def profile_for_domain(domain):
    d = norm_domain(domain)
    for key, prof in DOMAIN_PROFILE.items():
        if d == key or d.endswith('.' + key) or key in d:
            return prof
    return DEFAULT_PROFILE   # article directories + TBD -> SEO-depth (intentional policy)


def split_terms(s):
    parts = re.split(r'[,\n;/]+', s or '')
    out = []
    for p in parts:
        p = p.strip(' .\t\r"\'')
        if p and not re.fullmatch(r'(?i)(primary|secondary)', p):
            out.append(p)
    return out


def parse_keywords(cell):
    text = (cell or '').strip()
    if not text:
        return [], [], ['empty keyword cell']
    flags = []
    parts = re.split(r'(?i)\bsecondary\b\s*[:\-]?', text, maxsplit=1)
    if len(parts) == 2:
        primary_part = re.sub(r'(?i)\bprimary\b\s*[:\-]?', '', parts[0])
        primary = split_terms(primary_part)
        secondary = split_terms(parts[1])
    else:
        primary_part = re.sub(r'(?i)\bprimary\b\s*[:\-]?', '', text)
        primary = split_terms(primary_part)
        secondary = []
        if not re.search(r'(?i)\bprimary\b', text):
            flags.append('no Primary/Secondary labels; treated all as primary')
    if not primary:
        flags.append('no primary keyword parsed')
    return primary, secondary, flags


def detect_columns(header_cells):
    headers = [(i, str(c).strip().lower() if c is not None else '') for i, c in enumerate(header_cells)]
    col = {}
    for field, aliases in HEADER_ALIASES.items():
        for alias in aliases:                       # longest/most specific first
            for i, h in headers:
                if h == alias or alias in h:
                    col[field] = i
                    break
            if field in col:
                break
    return col


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    import openpyxl
    path = sys.argv[1]
    make_manifest = '--manifest' in sys.argv[2:]

    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        print(json.dumps({'error': 'empty sheet'}))
        sys.exit(1)

    col = detect_columns(rows[0])
    missing = [f for f in ('domain', 'title', 'keywords') if f not in col]
    if missing:
        print(json.dumps({'error': f'could not find columns: {missing}', 'detected': col,
                          'header_row': [str(c) for c in rows[0]]}, indent=2))
        sys.exit(1)

    records = []
    for n, r in enumerate(rows[1:], start=2):    # 1-based incl header
        def cell(field):
            i = col.get(field)
            return r[i] if i is not None and i < len(r) else None
        domain = cell('domain')
        title = cell('title')
        if not (domain or title):
            continue                              # skip blank rows
        profile = profile_for_domain(domain)
        primary, secondary, kflags = parse_keywords(cell('keywords'))
        flags = list(kflags)   # blocking issues only -> drive 'needs-review'
        if not title:
            flags.append('missing title')
        records.append({
            'row': n,
            'domain': norm_domain(domain),
            'platform': profile,
            'title': (title or '').strip(),
            'primary_keywords': primary,
            'secondary_keywords': secondary,
            'brief': (cell('brief') or '').strip(),
            'flags': flags,
        })

    print(json.dumps(records, indent=2, ensure_ascii=False))

    if make_manifest:
        mpath = os.path.splitext(path)[0] + '_manifest.csv'
        done = {}
        if os.path.exists(mpath):                 # preserve prior statuses -> resumable
            with open(mpath, newline='', encoding='utf-8') as f:
                for row in csv.DictReader(f):
                    done[row.get('row')] = row
        with open(mpath, 'w', newline='', encoding='utf-8') as f:
            w = csv.writer(f)
            w.writerow(['row', 'platform', 'title', 'status', 'cta_url', 'output_file', 'notes'])
            for rec in records:
                prev = done.get(str(rec['row']), {})
                status = prev.get('status') or ('needs-review' if rec['flags'] else 'pending')
                w.writerow([rec['row'], rec['platform'], rec['title'], status,
                            prev.get('cta_url', ''), prev.get('output_file', ''),
                            prev.get('notes', '; '.join(rec['flags']))])
        sys.stderr.write(f"\nManifest written: {mpath} ({len(records)} rows)\n")


if __name__ == '__main__':
    main()
