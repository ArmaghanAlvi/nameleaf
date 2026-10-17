-- recall_case.sql -- one filtered-recall probe case (PARTITION C6).
-- READ-ONLY: runs inside BEGIN ... ROLLBACK in a session started with
-- default_transaction_read_only=on (recall_check.sh, via lib.sh psql_ro).
--
-- Variables supplied by recall_check.sh:
--   sid        the probe (seed) sense id; its STORED embedding is the query
--              vector (psql has no model, so this differs from the app,
--              which embeds a query text; same as C-5.10's fann.sh)
--   lang       the target language code
--   do_strict  true/false: run the fallback-rung list (cross-language only)
--   explain    '' to run the queries, or 'EXPLAIN (COSTS OFF)' to print plans
--
-- Each list is printed between "@@begin <list>" and "@@end": rows of
-- sense_id <TAB> distance, or the EXPLAIN text. "@@ms <list> <ms>" follows
-- each run (meaningless in the EXPLAIN pass).
--
-- Constants (tests/test_publish_scripts.py ties each to its Python source):
\set max_scan_tuples 20000
\set ef_search 100
\set k_traversal 150
\set k_continuity 15
\set k_fallback 1
\set model intfloat/multilingual-e5-base

\set ON_ERROR_STOP on
BEGIN;
SET LOCAL lock_timeout = '30s';

-- Traversal lists: mirror vector_sense_search.expand_from_selected_senses's
-- candidate query (model, visible, tree language, every sense of the seed's
-- lexeme excluded) under scoped_vector_scan(mode="relaxed_order").
SET LOCAL hnsw.iterative_scan = relaxed_order;
SET LOCAL hnsw.max_scan_tuples = :max_scan_tuples;
SET LOCAL hnsw.ef_search = :ef_search;

\echo @@begin ann150
SELECT clock_timestamp() AS t0 \gset
:explain SELECT se.sense_id, se.embedding <=> (SELECT q.embedding FROM sense_embeddings q WHERE q.sense_id = :sid AND q.embedding_model = :'model') AS d
  FROM sense_embeddings se
  JOIN senses s ON s.id = se.sense_id
  JOIN lexemes l ON l.id = s.lexeme_id
  JOIN languages g ON g.id = l.language_id
 WHERE se.embedding_model = :'model'
   AND s.visibility_status = 'visible'
   AND g.code = :'lang'
   AND se.sense_id NOT IN (SELECT x.id FROM senses x WHERE x.lexeme_id = (SELECT y.lexeme_id FROM senses y WHERE y.id = :sid))
 ORDER BY se.embedding <=> (SELECT q.embedding FROM sense_embeddings q WHERE q.sense_id = :sid AND q.embedding_model = :'model')
 LIMIT :k_traversal;
SELECT round(1000 * extract(epoch FROM clock_timestamp() - :'t0'::timestamptz)) AS ms \gset
\echo @@end
\echo @@ms ann150 :ms

\echo @@begin ann15
SELECT clock_timestamp() AS t0 \gset
:explain SELECT se.sense_id, se.embedding <=> (SELECT q.embedding FROM sense_embeddings q WHERE q.sense_id = :sid AND q.embedding_model = :'model') AS d
  FROM sense_embeddings se
  JOIN senses s ON s.id = se.sense_id
  JOIN lexemes l ON l.id = s.lexeme_id
  JOIN languages g ON g.id = l.language_id
 WHERE se.embedding_model = :'model'
   AND s.visibility_status = 'visible'
   AND g.code = :'lang'
   AND se.sense_id NOT IN (SELECT x.id FROM senses x WHERE x.lexeme_id = (SELECT y.lexeme_id FROM senses y WHERE y.id = :sid))
 ORDER BY se.embedding <=> (SELECT q.embedding FROM sense_embeddings q WHERE q.sense_id = :sid AND q.embedding_model = :'model')
 LIMIT :k_continuity;
SELECT round(1000 * extract(epoch FROM clock_timestamp() - :'t0'::timestamptz)) AS ms \gset
\echo @@end
\echo @@ms ann15 :ms

-- Fallback-rung list: mirrors root_selection._vector_rung (strict_order,
-- LIMIT 1, visible + language, no model filter on the candidates, no
-- exclusion). Cross-language cases only: the rung serves non-English roots.
\if :do_strict
SET LOCAL hnsw.iterative_scan = strict_order;
\echo @@begin ann1
SELECT clock_timestamp() AS t0 \gset
:explain SELECT se.sense_id, se.embedding <=> (SELECT q.embedding FROM sense_embeddings q WHERE q.sense_id = :sid AND q.embedding_model = :'model') AS d
  FROM senses s
  JOIN sense_embeddings se ON se.sense_id = s.id
  JOIN lexemes l ON l.id = s.lexeme_id
  JOIN languages g ON g.id = l.language_id
 WHERE g.code = :'lang'
   AND s.visibility_status = 'visible'
 ORDER BY se.embedding <=> (SELECT q.embedding FROM sense_embeddings q WHERE q.sense_id = :sid AND q.embedding_model = :'model')
 LIMIT :k_fallback;
SELECT round(1000 * extract(epoch FROM clock_timestamp() - :'t0'::timestamptz)) AS ms \gset
\echo @@end
\echo @@ms ann1 :ms
\endif

-- Exact: the traversal query with index scans disabled (HNSW supports only
-- ordered index scans, so this forces a full distance sort; bitmap scans
-- stay allowed so the btree joins stay cheap). Ordered by (distance,
-- sense_id), so ties have a deterministic order.
SET LOCAL enable_indexscan = off;
SET LOCAL enable_indexonlyscan = off;
\echo @@begin exact
SELECT clock_timestamp() AS t0 \gset
:explain SELECT se.sense_id, se.embedding <=> (SELECT q.embedding FROM sense_embeddings q WHERE q.sense_id = :sid AND q.embedding_model = :'model') AS d
  FROM sense_embeddings se
  JOIN senses s ON s.id = se.sense_id
  JOIN lexemes l ON l.id = s.lexeme_id
  JOIN languages g ON g.id = l.language_id
 WHERE se.embedding_model = :'model'
   AND s.visibility_status = 'visible'
   AND g.code = :'lang'
   AND se.sense_id NOT IN (SELECT x.id FROM senses x WHERE x.lexeme_id = (SELECT y.lexeme_id FROM senses y WHERE y.id = :sid))
 ORDER BY 2, 1
 LIMIT :k_traversal;
SELECT round(1000 * extract(epoch FROM clock_timestamp() - :'t0'::timestamptz)) AS ms \gset
\echo @@end
\echo @@ms exact :ms

ROLLBACK;
