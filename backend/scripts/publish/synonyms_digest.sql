-- Synonyms digest (PARTITION C4). READ-ONLY. Run on master and on any
-- slimmed copy: the two results must be identical.
--
-- KEYS: synonyms   (== sense_embeddings._EMBEDDABLE_SYNONYM_KEYS; tested)
--
-- Per row, the text that projection.sql preserves:
--   object      -> id, 'o', key=value for each listed key in stored order
--   non-object  -> id, json_typeof, the whole value (projection leaves it)
-- Each row is md5'd; the table digest is md5 over the row hashes ordered by
-- id (row hashes keep the aggregate ~33 bytes/row, far below Postgres's 1 GB
-- text limit). Any change to a synonyms value, or a listed key appearing or
-- disappearing, changes the digest. Output: label, rows, digest.

WITH r AS (
    SELECT s.id,
           md5(s.id::text || E'\t' ||
               CASE WHEN json_typeof(s.raw_sense) = 'object' THEN
                   'o' || E'\t' || COALESCE(
                       (SELECT string_agg(e.key || '=' || e.value::text, E'\x1f'
                                          ORDER BY e.ord)
                          FROM json_each(s.raw_sense) WITH ORDINALITY
                               AS e(key, value, ord)
                         WHERE e.key IN ('synonyms')), '')
               ELSE COALESCE(json_typeof(s.raw_sense), 'sqlnull') || E'\t' ||
                    COALESCE(s.raw_sense::text, '')
               END) AS h
      FROM senses s
)
SELECT 'senses_synonyms', count(*), md5(string_agg(h, '' ORDER BY id)) FROM r;

WITH r AS (
    SELECT l.id,
           md5(l.id::text || E'\t' ||
               CASE WHEN json_typeof(l.raw_entry) = 'object' THEN
                   'o' || E'\t' || COALESCE(
                       (SELECT string_agg(e.key || '=' || e.value::text, E'\x1f'
                                          ORDER BY e.ord)
                          FROM json_each(l.raw_entry) WITH ORDINALITY
                               AS e(key, value, ord)
                         WHERE e.key IN ('synonyms')), '')
               ELSE COALESCE(json_typeof(l.raw_entry), 'sqlnull') || E'\t' ||
                    COALESCE(l.raw_entry::text, '')
               END) AS h
      FROM lexemes l
)
SELECT 'lexemes_synonyms', count(*), md5(string_agg(h, '' ORDER BY id)) FROM r;
