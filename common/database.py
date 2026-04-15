"""
Database access layer.

Exposes a module-level connection pool and small helper functions that
cover the common query shapes agents will use:
  - get_conn() / put_conn()          — explicit pool checkout / checkin
  - connection()                     — context manager, preferred
  - fetch_one / fetch_all / execute  — one-shot helpers
  - record_agent_run()               — every agent calls this on completion

Keeping this thin on purpose: agents write their own SQL for domain-specific
queries. This module provides plumbing, not an ORM.
"""

from __future__ import annotations

import contextlib
import json
from typing import Any, Iterator, Sequence

import psycopg2
import psycopg2.extras
from psycopg2.pool import ThreadedConnectionPool

from common.config import settings


# Lazy pool — created on first use so importing this module doesn't
# require a running database (useful for unit tests that mock the DB).
_pool: ThreadedConnectionPool | None = None


def _get_pool() -> ThreadedConnectionPool:
    global _pool
    if _pool is None:
        _pool = ThreadedConnectionPool(
            minconn=settings.DB_POOL_MIN,
            maxconn=settings.DB_POOL_MAX,
            dsn=settings.DATABASE_URL,
        )
    return _pool


def close_pool() -> None:
    """Close all pooled connections. Called at process shutdown."""
    global _pool
    if _pool is not None:
        _pool.closeall()
        _pool = None


@contextlib.contextmanager
def connection(dict_cursor: bool = True) -> Iterator[psycopg2.extensions.connection]:
    """
    Check out a connection from the pool, commit on clean exit, roll back on exception.

        with connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT ...")
    """
    pool = _get_pool()
    conn = pool.getconn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        pool.putconn(conn)


def fetch_one(sql: str, params: Sequence[Any] | None = None) -> dict | None:
    with connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params or ())
            row = cur.fetchone()
            return dict(row) if row else None


def fetch_all(sql: str, params: Sequence[Any] | None = None) -> list[dict]:
    with connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params or ())
            return [dict(r) for r in cur.fetchall()]


def execute(sql: str, params: Sequence[Any] | None = None) -> int:
    """Execute a non-returning statement. Returns affected row count."""
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params or ())
            return cur.rowcount


def execute_many(sql: str, params_seq: Sequence[Sequence[Any]]) -> int:
    with connection() as conn:
        with conn.cursor() as cur:
            cur.executemany(sql, params_seq)
            return cur.rowcount


# ---------------------------------------------------------------------------
# Agent run tracking — every agent calls this so we have operational visibility
# without relying on log files.
# ---------------------------------------------------------------------------

def record_agent_run(
    agent_name: str,
    status: str,
    records_processed: int = 0,
    errors: list | None = None,
    duration_seconds: float | None = None,
    metadata: dict | None = None,
) -> int:
    """
    Insert a row into agent_runs. Returns the new run id.
    Status must be one of: running, success, error, partial.
    """
    sql = """
        INSERT INTO agent_runs
            (agent_name, status, records_processed, errors, duration_seconds, metadata)
        VALUES (%s, %s, %s, %s, %s, %s)
        RETURNING id
    """
    params = (
        agent_name,
        status,
        records_processed,
        psycopg2.extras.Json(errors or []),
        duration_seconds,
        psycopg2.extras.Json(metadata or {}),
    )
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchone()[0]


__all__ = [
    "close_pool",
    "connection",
    "execute",
    "execute_many",
    "fetch_all",
    "fetch_one",
    "record_agent_run",
]
