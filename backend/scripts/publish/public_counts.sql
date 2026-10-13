-- Exact row counts for every table in schema public (PARTITION C4).
-- READ-ONLY. Same technique as c1_master/rowcounts.sql. Output, ordered by
-- table name: table <TAB> rows (run with psql -At -F '<TAB>').
SELECT c.relname,
       (xpath('/row/c/text()',
              query_to_xml(format('SELECT count(*) AS c FROM %I.%I',
                                  n.nspname, c.relname),
                           false, true, '')))[1]::text::bigint
  FROM pg_class c
  JOIN pg_namespace n ON n.oid = c.relnamespace
 WHERE c.relkind = 'r'
   AND n.nspname = 'public'
 ORDER BY 1;
