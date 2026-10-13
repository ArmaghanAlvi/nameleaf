-- Slimming by projection (PARTITION C4). Runs ONLY in the throwaway build
-- database, between the data and post-data restore sections, so no index
-- (btree or HNSW) exists yet and the UPDATEs pay no index maintenance.
--
-- KEYS: synonyms
--   The key list must equal app/services/sense_embeddings.py
--   _EMBEDDABLE_SYNONYM_KEYS -- the only keys of raw_sense / raw_entry the
--   request path reads (build_query_text_from_selected_senses ->
--   collect_synonyms_for_embedding -> _kaikki_sense_level_synonyms /
--   _kaikki_entry_level_synonyms). tests/test_publish_scripts.py checks every
--   IN (...) list in this file and in synonyms_digest.sql against it.
--
-- Semantics, matched to the Python reader `(raw or {}).get(key) or []`:
--   * JSON object: keep exactly the listed keys, in their original order,
--     values verbatim (json, not jsonb: json_each returns the stored text);
--     a missing key is simply absent ({} if none remain); a key whose value
--     is JSON null stays null (Python treats both as "no synonyms").
--   * Not an object (JSON null, array, scalar): the row is left untouched.
--     Counted below; expected 0.
--   * Duplicate keys survive in order; Python's json.loads takes the last,
--     as it did before.
-- sense_embeddings.embedded_text is never read by the request path; it
-- becomes '' (the column is NOT NULL).

\echo 'non-object JSON rows (left untouched):'
SELECT 'senses.raw_sense', count(*) FROM senses
 WHERE json_typeof(raw_sense) IS DISTINCT FROM 'object';
SELECT 'lexemes.raw_entry', count(*) FROM lexemes
 WHERE json_typeof(raw_entry) IS DISTINCT FROM 'object';

\timing on

UPDATE senses s SET raw_sense = COALESCE(
    (SELECT json_object_agg(e.key, e.value ORDER BY e.ord)
       FROM json_each(s.raw_sense) WITH ORDINALITY AS e(key, value, ord)
      WHERE e.key IN ('synonyms')),
    '{}'::json)
 WHERE json_typeof(s.raw_sense) = 'object';

UPDATE lexemes l SET raw_entry = COALESCE(
    (SELECT json_object_agg(e.key, e.value ORDER BY e.ord)
       FROM json_each(l.raw_entry) WITH ORDINALITY AS e(key, value, ord)
      WHERE e.key IN ('synonyms')),
    '{}'::json)
 WHERE json_typeof(l.raw_entry) = 'object';

UPDATE sense_embeddings SET embedded_text = '' WHERE embedded_text <> '';
