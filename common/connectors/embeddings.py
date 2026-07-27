"""
Embeddings connector — semantic similarity for keyword work.
=============================================================

Why this exists
---------------
`trend_scout` decides whether a discovered phrase is genuinely new by
comparing it against the tracked keyword set. That comparison was token-set
Jaccard, which is blind to paraphrase: "ai agent orchestration platform" and
a tracked "agentic ai development services" share almost no tokens and scored
as unrelated, so the phrase was proposed as novel.

That is not a cosmetic miss. Every keyword promoted into `keywords` costs
money on every future rank-tracker run, forever. The novelty check is the
guard on that spend, and a guard that cannot see paraphrase leaks.

Why Voyage rather than Claude
-----------------------------
Anthropic does not expose an embeddings endpoint; Voyage is their recommended
partner. This is also the right shape of tool for the job — similarity is a
vector problem, and asking a chat model to compare each candidate against
2,126 tracked keywords would be quadratic and unaffordable.

Degradation
-----------
Never raises for a missing key or a failed call. `embed()` returns `None` and
callers fall back to their existing rule — same contract as `common/llm.py`.
The system must stay fully functional with no `VOYAGE_API_KEY` set.

Caching
-------
Embeddings are deterministic for a given (text, model), so they are cached in
`keyword_embeddings` and only computed once. The tracked keyword set is
embedded on first use; subsequent runs only pay for new candidate phrases.

Usage
-----
    from common.connectors.embeddings import embed, cosine, nearest

    vectors = embed(["agentic ai services", "salesforce consulting"])
    if vectors is None:
        ...        # unavailable — fall back to the rule
"""

from __future__ import annotations

import hashlib
import logging
import os
import time
from typing import Sequence

from common.config import settings


logger = logging.getLogger(__name__)

__all__ = [
    "embed",
    "embed_cached",
    "cosine",
    "nearest",
    "is_available",
    "EMBED_MODEL",
    "COST_PER_MILLION_TOKENS",
]

# voyage-3-lite: 512 dims, strong quality per dollar, ample for short keywords.
EMBED_MODEL = "voyage-3-lite"
EMBED_DIMS = 512

# Observed 2026-07. Keyword-length inputs are ~5 tokens each, so 2,126 tracked
# keywords is roughly 11k tokens — well under a cent.
COST_PER_MILLION_TOKENS = 0.02

BATCH_SIZE = 128
MAX_ATTEMPTS = 3
BACKOFF_BASE_SEC = 2.0


def _api_key() -> str | None:
    key = os.environ.get("VOYAGE_API_KEY") or getattr(settings, "VOYAGE_API_KEY", "")
    if not key or key in ("your_key_here", "pa-..."):
        return None
    return key


def is_available() -> bool:
    """True when embeddings can actually be computed."""
    if _api_key() is None:
        return False
    try:
        import voyageai  # noqa: F401
    except ImportError:
        return False
    return True


def embed(texts: Sequence[str], *, input_type: str = "document") -> list[list[float]] | None:
    """
    Embed a list of strings. Returns vectors in input order, or None when
    embeddings are unavailable for any reason.

    `input_type` is Voyage's asymmetric-retrieval hint. Keyword-to-keyword
    similarity is symmetric, so "document" for both sides is correct here —
    using "query" on one side would skew the geometry.
    """
    texts = [t for t in texts]
    if not texts:
        return []

    key = _api_key()
    if key is None:
        logger.debug("VOYAGE_API_KEY not set — semantic similarity unavailable")
        return None

    try:
        import voyageai
    except ImportError:
        logger.warning("voyageai SDK not installed — semantic similarity unavailable "
                       "(pip install voyageai)")
        return None

    client = voyageai.Client(api_key=key)
    out: list[list[float]] = []

    for start in range(0, len(texts), BATCH_SIZE):
        batch = texts[start:start + BATCH_SIZE]
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                resp = client.embed(batch, model=EMBED_MODEL, input_type=input_type)
                out.extend(resp.embeddings)
                break
            except Exception as exc:                      # SDK raises a wide range
                if attempt == MAX_ATTEMPTS:
                    logger.warning("Voyage embed failed after %d attempts (%s) — "
                                   "falling back to the rule", MAX_ATTEMPTS, exc)
                    return None
                delay = BACKOFF_BASE_SEC * (2 ** (attempt - 1))
                logger.warning("Voyage embed attempt %d/%d failed (%s) — retrying in %.0fs",
                               attempt, MAX_ATTEMPTS, exc, delay)
                time.sleep(delay)

    return out


# ---------------------------------------------------------------------------
# Caching
# ---------------------------------------------------------------------------

def _text_hash(text: str) -> str:
    return hashlib.sha256(f"{EMBED_MODEL}\x00{text.strip().lower()}".encode()).hexdigest()


def embed_cached(texts: Sequence[str]) -> dict[str, list[float]] | None:
    """
    Embed with a database cache. Returns {text: vector}, or None when
    embeddings are unavailable AND nothing was cached.

    A partial result is better than nothing: if the API is down but the
    tracked keywords were embedded on a previous run, the caller still gets
    those and can compare against them.
    """
    from common.database import connection, fetch_all

    wanted = {t.strip(): _text_hash(t) for t in texts if t and t.strip()}
    if not wanted:
        return {}

    hashes = list(wanted.values())
    cached_rows = fetch_all(
        "SELECT text_hash, embedding FROM keyword_embeddings "
        " WHERE model = %s AND text_hash = ANY(%s)",
        [EMBED_MODEL, hashes],
    )
    by_hash = {r["text_hash"]: r["embedding"] for r in cached_rows}

    missing = [t for t, h in wanted.items() if h not in by_hash]
    if missing:
        fresh = embed(missing)
        if fresh is None:
            if not by_hash:
                return None
            logger.info("Embeddings unavailable; using %d cached vector(s), "
                        "%d phrase(s) will fall back to the rule",
                        len(by_hash), len(missing))
        else:
            with connection() as conn:
                with conn.cursor() as cur:
                    for text, vector in zip(missing, fresh):
                        cur.execute(
                            """
                            INSERT INTO keyword_embeddings (text, text_hash, model, embedding)
                            VALUES (%s, %s, %s, %s)
                            ON CONFLICT (text_hash, model) DO NOTHING
                            """,
                            (text[:500], wanted[text], EMBED_MODEL, vector),
                        )
                        by_hash[wanted[text]] = vector
                conn.commit()

    return {t: by_hash[h] for t, h in wanted.items() if h in by_hash}


# ---------------------------------------------------------------------------
# Similarity
# ---------------------------------------------------------------------------

def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """
    Cosine similarity. Voyage returns L2-normalized vectors, so this is a dot
    product — but normalize anyway rather than depend on that guarantee.
    """
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def nearest(target: Sequence[float],
            candidates: dict[str, Sequence[float]]) -> tuple[str | None, float]:
    """Closest candidate to `target` by cosine. Returns (label, similarity)."""
    best_label: str | None = None
    best_score = 0.0
    for label, vector in candidates.items():
        score = cosine(target, vector)
        if score > best_score:
            best_label, best_score = label, score
    return best_label, best_score
