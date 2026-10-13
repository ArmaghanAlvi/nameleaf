-- check_restore.sql -- facts about a restored database (PARTITION C4).
-- READ-ONLY. check_restore.sh runs it and compares the output with the build
-- manifest. Output: key <TAB> value (run with psql -At -F '<TAB>').

SELECT 'alembic', version_num FROM public.alembic_version;
SELECT 'established_names', count(*) FROM public.established_names;
SELECT 'live_schema', count(*) FROM pg_namespace WHERE nspname = 'live';

SELECT 'live_rows:' || c.relname,
       (xpath('/row/c/text()',
              query_to_xml(format('SELECT count(*) AS c FROM live.%I', c.relname),
                           false, true, '')))[1]::text::bigint
  FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
 WHERE n.nspname = 'live' AND c.relkind = 'r'
 ORDER BY 1;

-- Statistics rows whose sense no longer exists (no FK from live into public
-- since C2). Reported, not a failure: readers tolerate them.
SELECT 'orphan_stats', count(*) FROM live.sense_selection_stats s
 WHERE NOT EXISTS (SELECT 1 FROM public.senses x WHERE x.id = s.sense_id);
SELECT 'orphan_events', count(*) FROM live.sense_selection_events e
 WHERE NOT EXISTS (SELECT 1 FROM public.senses x WHERE x.id = e.sense_id);

SELECT 'pgvector', extversion FROM pg_extension WHERE extname = 'vector';
SELECT 'hnsw_indexes', count(*)
  FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid
  JOIN pg_am a ON a.oid = c.relam
 WHERE i.indrelid = 'public.sense_embeddings'::regclass AND a.amname = 'hnsw';
SELECT 'invalid_indexes', count(*) FROM pg_index i
  JOIN pg_class c ON c.oid = i.indexrelid
  JOIN pg_namespace n ON n.oid = c.relnamespace
 WHERE n.nspname IN ('public', 'live') AND NOT i.indisvalid;
