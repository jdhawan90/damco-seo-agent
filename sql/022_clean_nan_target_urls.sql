-- ============================================================================
-- Clear literal 'NaN' target URLs (migration 022)
--
-- Nine keywords, all Data Engineering, carry the four-character string 'NaN'
-- in `keywords.target_url`. That is a pandas artifact — an empty cell in a
-- spreadsheet import became the float NaN and was then written as text.
--
-- It is not harmless. The dashboard's "a different page is ranking" tile
-- compares `url_found` to `target_url`, and 'NaN' can never equal a real URL,
-- so those nine appear as permanent false positives in a list meant to be
-- acted on. A list with known-bad rows in it stops being read.
--
-- NULL is the correct representation of "no page assigned". The tile counts
-- those separately and says so.
--
-- Idempotent.
-- ============================================================================

BEGIN;

UPDATE keywords
   SET target_url = NULL,
       updated_at = now()
 WHERE target_url IS NOT NULL
   AND btrim(target_url) IN ('NaN', 'nan', 'NULL', 'null', 'None', '-', '');

COMMIT;
