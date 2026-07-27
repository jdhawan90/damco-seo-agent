-- ============================================================================
-- Damco SEO AI Agent System — content hash versioning (migration 016)
--
-- competitor_monitor's `content_hash` changed meaning.
--
-- It used to be a proxy: sha256("wc=<n>|<title>|<meta>|<h1>"). That could not
-- detect a body rewrite — a competitor could replace every paragraph, land on
-- the same word count and keep the title, and `content_update` would never
-- fire. For an agent whose entire job is detecting competitor content
-- changes, that was the one thing it most needed to catch.
--
-- It is now sha256 of the page's normalized visible text, computed by the
-- crawler (`CrawlResult.text_hash`).
--
-- The problem this migration solves
-- ---------------------------------
-- Changing the algorithm means every one of the 52 stored pages will hash
-- differently on the next crawl, and every one would fire a spurious
-- `content_update` event. The digest would report 52 competitor content
-- changes that never happened, on the same day the fix landed — and those
-- events are append-only, so the noise would be permanent.
--
-- Recording which algorithm produced each stored hash lets the next run
-- re-baseline silently: a hash computed under a different algorithm is not
-- comparable, so the monitor stores the new value and emits nothing.
--
-- Existing rows are marked 'proxy_v1'. New writes use 'text_v2'.
--
-- Idempotent.
-- ============================================================================

BEGIN;

ALTER TABLE competitor_pages
    ADD COLUMN IF NOT EXISTS content_hash_algo TEXT NOT NULL DEFAULT 'proxy_v1';

COMMENT ON COLUMN competitor_pages.content_hash_algo IS
    'Which algorithm produced content_hash. Hashes from different algorithms '
    'are not comparable — competitor_monitor re-baselines instead of firing a '
    'change event when this does not match the current algorithm.';

-- Be explicit rather than relying on the DEFAULT: rows written before this
-- migration were all produced by the proxy.
UPDATE competitor_pages
   SET content_hash_algo = 'proxy_v1'
 WHERE content_hash_algo IS NULL;

COMMIT;
