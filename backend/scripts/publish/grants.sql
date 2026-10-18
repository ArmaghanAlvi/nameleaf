-- grants.sql -- per-database privileges (PARTITION C4, Stage 2d).
-- Run for EVERY new published database, after its public schema is restored
-- and its live schema created; re-runnable (idempotent). Requires roles.sql.
-- Run as the superuser, connected to the new database, with -v db=<name>:
--   docker exec -i -e PGOPTIONS='-c lock_timeout=30s' <container> \
--     psql -X -v ON_ERROR_STOP=1 -U postgres -d <db> -v db=<db> < grants.sql
--
-- nameleaf_app gets exactly: CONNECT; USAGE + SELECT on everything in
-- public; USAGE on live with SELECT/INSERT/UPDATE on its tables and USAGE on
-- its sequences (record_sense_selection reads, then inserts or updates; the
-- dropdown reads); default privileges for future live tables and sequences
-- created by nameleaf_admin. Nothing else: no DELETE/TRUNCATE anywhere, no
-- writes to public, no CREATE, no TEMP.

\set ON_ERROR_STOP on
SET lock_timeout = '30s';

\if :{?db}
\else
  DO $$ BEGIN RAISE EXCEPTION 'run grants.sql with -v db=<database>'; END $$;
\endif

SELECT current_database() = :'db' AS right_db \gset
\if :right_db
\else
  DO $$ BEGIN RAISE EXCEPTION 'connected database does not match -v db'; END $$;
\endif

REVOKE ALL ON DATABASE :"db" FROM PUBLIC;
GRANT CONNECT ON DATABASE :"db" TO nameleaf_app;

REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO nameleaf_app;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO nameleaf_app;
GRANT SELECT ON ALL SEQUENCES IN SCHEMA public TO nameleaf_app;

GRANT USAGE ON SCHEMA live TO nameleaf_app;
GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA live TO nameleaf_app;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA live TO nameleaf_app;
ALTER DEFAULT PRIVILEGES FOR ROLE nameleaf_admin IN SCHEMA live
    GRANT SELECT, INSERT, UPDATE ON TABLES TO nameleaf_app;
ALTER DEFAULT PRIVILEGES FOR ROLE nameleaf_admin IN SCHEMA live
    GRANT USAGE ON SEQUENCES TO nameleaf_app;

\echo 'grants applied'
