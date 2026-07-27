-- ============================================================================
-- Damco SEO AI Agent System — embedding cache (migration 015)
--
-- Backs common/connectors/embeddings.py.
--
-- Why cache
-- ---------
-- Embeddings are deterministic for a given (text, model), and the tracked
-- keyword set barely changes between runs. Without a cache, every trend run
-- would re-embed all ~2,126 tracked keywords to compare a few dozen new
-- candidates against them. With it, the tracked set is paid for once and each
-- run only embeds genuinely new phrases.
--
-- The cache is also what makes the connector degrade well: if Voyage is
-- unreachable mid-run, previously embedded vectors are still available and
-- the comparison still works for everything already seen.
--
-- Storage choice
-- --------------
-- REAL[] rather than the pgvector `vector` type. pgvector would enable
-- indexed nearest-neighbour search, but that matters at millions of rows —
-- here the whole set is a few thousand short strings compared in Python, and
-- requiring an extension would add an install step to every environment for
-- no measurable gain. Revisit if this table ever gets large.
--
-- Idempotent.
-- ============================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS keyword_embeddings (
    id         BIGSERIAL PRIMARY KEY,

    -- Truncated for storage; text_hash is computed from the full normalized
    -- string, so truncation never causes a collision or a wrong cache hit.
    text       TEXT        NOT NULL,

    -- sha256 of "<model>\0<lowercased, stripped text>". Including the model
    -- means switching models invalidates cleanly instead of silently mixing
    -- vector spaces, which would produce meaningless similarities.
    text_hash  TEXT        NOT NULL,
    model      TEXT        NOT NULL,

    embedding  REAL[]      NOT NULL,

    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    UNIQUE (text_hash, model)
);

CREATE INDEX IF NOT EXISTS idx_keyword_embeddings_model
    ON keyword_embeddings (model);

COMMENT ON TABLE keyword_embeddings IS
    'Cached embedding vectors. Populated by common/connectors/embeddings.py; '
    'safe to TRUNCATE — it is a pure cache and will refill on the next run.';

COMMIT;
