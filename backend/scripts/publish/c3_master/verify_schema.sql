SELECT 'table', n.nspname || '.' || c.relname
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind = 'r'
  AND c.relname IN ('sense_selection_stats', 'sense_selection_events',
                    'word_search_stats', 'word_search_events', 'root_llm_attempts')
ORDER BY 2;
SELECT 'fk', conrelid::regclass::text, conname
FROM pg_constraint
WHERE contype = 'f' AND conrelid::regclass::text ~ '(sense_selection_|word_search_)'
ORDER BY 3;
SELECT 'schema_live', count(*) FROM pg_namespace WHERE nspname = 'live';
SELECT 'alembic', version_num FROM alembic_version;
