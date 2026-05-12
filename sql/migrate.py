"""
Minimal migration runner for Damco SEO Agent System.

Applies any .sql files in ./sql/ (sorted by filename) that haven't been
applied yet. Tracked state is stored in a schema_migrations table inside
the target database itself — idempotent and safe to re-run.

Usage
-----
    python sql/migrate.py

Reads DATABASE_URL from .env (or the environment). No arguments needed.
"""

from __future__ import annotations

import glob
import os
import sys
from pathlib import Path

import psycopg2
from dotenv import load_dotenv


MIGRATIONS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    filename    TEXT         PRIMARY KEY,
    applied_at  TIMESTAMPTZ  NOT NULL DEFAULT now()
);
"""


def main() -> int:
    # Load .env from repo root regardless of where we're invoked from.
    # override=True ensures the .env value wins over any pre-existing
    # shell env var (see common/config.py for the full rationale).
    repo_root = Path(__file__).resolve().parent.parent
    load_dotenv(repo_root / ".env", override=True)

    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        print("ERROR: DATABASE_URL not set. Copy .env.example to .env and fill it in.", file=sys.stderr)
        return 1

    sql_dir = repo_root / "sql"
    migration_files = sorted(glob.glob(str(sql_dir / "*.sql")))
    if not migration_files:
        print("No migration files found in sql/.")
        return 0

    conn = psycopg2.connect(db_url)
    conn.autocommit = False
    try:
        with conn.cursor() as cur:
            cur.execute(MIGRATIONS_TABLE_SQL)
            conn.commit()

            cur.execute("SELECT filename FROM schema_migrations")
            applied = {row[0] for row in cur.fetchall()}

        applied_count = 0
        for path in migration_files:
            filename = os.path.basename(path)
            if filename in applied:
                print(f"  skip   {filename}  (already applied)")
                continue

            print(f"  apply  {filename}")
            with open(path, "r", encoding="utf-8") as f:
                sql = f.read()

            with conn.cursor() as cur:
                cur.execute(sql)
                cur.execute(
                    "INSERT INTO schema_migrations (filename) VALUES (%s)",
                    (filename,),
                )
            conn.commit()
            applied_count += 1

        print(f"\nDone. {applied_count} migration(s) applied, {len(applied)} already in place.")
        return 0

    except Exception as exc:
        conn.rollback()
        print(f"\nERROR: migration failed and was rolled back: {exc}", file=sys.stderr)
        return 2
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
