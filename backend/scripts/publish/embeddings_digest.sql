-- Embeddings digest (PARTITION C4, addition A). READ-ONLY. Run on master, on
-- the build database and on every restored copy: all must be identical.
--
-- WHY: the HNSW index is rebuilt, never copied, at every restore, and the
-- vector search uses an approximate iterative scan; pgvector's level
-- assignment is, as far as we know, random and not seedable. So a restored
-- copy can return slightly different search results even when its data is
-- identical. Equal row counts + equal synonyms digests + an equal embeddings
-- digest prove the DATA is identical; a gate diff on such a copy then comes
-- from the index rebuild (or arm64 floats in an in-image run), not from
-- slimming or restore.
--
-- Per row: md5(sense_id, embedding_model, embedding::text); the digest is md5
-- over row hashes ordered by (sense_id, embedding_model). Output: label,
-- rows, digest.

WITH r AS (
    SELECT sense_id, embedding_model,
           md5(sense_id::text || E'\t' || embedding_model || E'\t' ||
               embedding::text) AS h
      FROM sense_embeddings
)
SELECT 'sense_embeddings_vectors', count(*),
       md5(string_agg(h, '' ORDER BY sense_id, embedding_model))
  FROM r;
