"""
One-time seed script: import existing keyword + ranking data into the database.

Reads the legacy CSV files from the old rank_tracker and imports them into
the keywords and keyword_rankings tables. Safe to re-run — uses ON CONFLICT
to skip duplicates.

Usage
-----
    python -m keyword_intelligence.seed_keywords [--csv PATH] [--results PATH]

Defaults to the legacy paths relative to this repo's parent directory.
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import sys
from datetime import date
from pathlib import Path

# Ensure the repo root is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from common.database import connection, fetch_all


# ---------------------------------------------------------------------------
# Offering inference — maps keyword text to Damco offerings
# ---------------------------------------------------------------------------

OFFERING_RULES: list[tuple[re.Pattern, str]] = [
    (re.compile(r"insurance|broker|broking|policy management", re.I), "Insurance Broker Software"),
    (re.compile(r"ai agent|ai consulting|ai development|ai application|ai ml|generative ai", re.I), "AI Development"),
    (re.compile(r"as400|iseries|ibm i", re.I), "AS400 Modernization"),
    (re.compile(r"data engineer|data collect|data annot", re.I), "Data Engineering"),
    (re.compile(r"gcp|google cloud|azure|aws", re.I), "Cloud Services"),
    (re.compile(r"salesforce", re.I), "Salesforce"),
]


def infer_offering(keyword: str) -> str:
    for pattern, offering in OFFERING_RULES:
        if pattern.search(keyword):
            return offering
    return "General"


# ---------------------------------------------------------------------------
# Bucket logic (shared with rank_tracker)
# ---------------------------------------------------------------------------

def rank_bucket(position: int | None) -> str:
    if position is None:
        return "not-found"
    if position <= 5:
        return "1-5"
    if position <= 10:
        return "5-10"
    if position <= 20:
        return "10-20"
    if position <= 50:
        return "20-50"
    return "50+"


def parse_position(val: str) -> int | None:
    """Parse a position from the legacy CSV. Returns None for 'Not in top N' or empty."""
    val = val.strip()
    if not val:
        return None
    try:
        return int(val)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Main import
# ---------------------------------------------------------------------------

def seed(keywords_csv: str | None, results_csv: str) -> None:
    # --- 1. Load legacy results CSV -----------------------------------------
    if not os.path.exists(results_csv):
        print(f"ERROR: Results CSV not found: {results_csv}")
        sys.exit(1)

    with open(results_csv, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        fieldnames = list(reader.fieldnames or [])
        # All columns after "Keyword" are date columns
        date_columns = [c for c in fieldnames if c != "Keyword"]
        rows = list(reader)

    print(f"Loaded {len(rows)} keywords with {len(date_columns)} date snapshots from {results_csv}")

    # --- 2. Also load keywords.csv if provided (may have extra keywords) ----
    extra_keywords: set[str] = set()
    if keywords_csv and os.path.exists(keywords_csv):
        with open(keywords_csv, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for row in reader:
                for key in row:
                    if key.strip().lower() == "keyword":
                        kw = row[key].strip()
                        if kw:
                            extra_keywords.add(kw)
        print(f"Loaded {len(extra_keywords)} keywords from {keywords_csv}")

    # Merge: all keywords from results + any extras from keywords.csv
    all_keywords: dict[str, dict[str, str]] = {}
    for row in rows:
        kw = row.get("Keyword", "").strip()
        if kw:
            all_keywords[kw] = {dc: row.get(dc, "") for dc in date_columns}
    for kw in extra_keywords:
        if kw not in all_keywords:
            all_keywords[kw] = {}

    print(f"Total unique keywords: {len(all_keywords)}")

    # --- 3. Upsert into keywords table --------------------------------------
    kw_insert = 0
    kw_skip = 0
    with connection() as conn:
        with conn.cursor() as cur:
            for kw in all_keywords:
                offering = infer_offering(kw)
                cur.execute("""
                    INSERT INTO keywords (keyword, offering, type, status)
                    VALUES (%s, %s, 'primary', 'active')
                    ON CONFLICT (keyword, offering) DO NOTHING
                    RETURNING id
                """, (kw, offering))
                row = cur.fetchone()
                if row:
                    kw_insert += 1
                else:
                    kw_skip += 1

    print(f"Keywords: {kw_insert} inserted, {kw_skip} already existed")

    # --- 4. Load keyword IDs back -------------------------------------------
    kw_rows = fetch_all("SELECT id, keyword FROM keywords")
    kw_id_map = {r["keyword"]: r["id"] for r in kw_rows}

    # --- 5. Insert historical rankings --------------------------------------
    rank_insert = 0
    rank_skip = 0
    with connection() as conn:
        with conn.cursor() as cur:
            for kw, dates in all_keywords.items():
                kw_id = kw_id_map.get(kw)
                if not kw_id:
                    continue
                for date_str, val in dates.items():
                    pos = parse_position(val)
                    try:
                        d = date.fromisoformat(date_str.strip())
                    except ValueError:
                        continue
                    cur.execute("""
                        INSERT INTO keyword_rankings
                            (keyword_id, date, rank_position, rank_bucket, source)
                        VALUES (%s, %s, %s, %s, 'manual')
                        ON CONFLICT (keyword_id, date, source) DO NOTHING
                        RETURNING id
                    """, (kw_id, d, pos, rank_bucket(pos)))
                    row = cur.fetchone()
                    if row:
                        rank_insert += 1
                    else:
                        rank_skip += 1

    print(f"Rankings: {rank_insert} inserted, {rank_skip} already existed")
    print("\nSeed complete.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Import legacy CSV keyword data into the database")
    default_base = Path(__file__).resolve().parent.parent.parent / "damco-rank-tracker"
    parser.add_argument("--csv", default=str(default_base / "keywords.csv"),
                        help="Path to keywords.csv")
    parser.add_argument("--results", default=str(default_base / "rank_results.csv"),
                        help="Path to rank_results.csv")
    args = parser.parse_args()

    seed(args.csv, args.results)


if __name__ == "__main__":
    main()
