-- roles.sql -- least-privilege roles (PARTITION C4, Stage 2d).
-- Cluster-wide; run ONCE per Postgres cluster, re-runnable (idempotent).
-- Per-database grants are in grants.sql, run for every new database.
--
--   nameleaf_admin  LOGIN; owns every restored object (the restore runs
--                    pg_restore --no-owner --role=nameleaf_admin, and the
--                    database is created OWNER nameleaf_admin).
--   nameleaf_app    LOGIN; what the backend connects as (grants.sql gives
--                    it read-only public, SELECT/INSERT/UPDATE on live).
--
-- Passwords are NEVER in this file or the repo. They are read from the
-- environment of the psql process with \getenv; pass them into the
-- container with `docker exec -e NAMELEAF_ADMIN_PASSWORD -e
-- NAMELEAF_APP_PASSWORD ...`, which copies the host values without putting
-- them on any command line. A role that does not exist yet needs its
-- password; an existing role's password is rotated only if one is given.
--
-- Run as the superuser, e.g. (see README.md):
--   docker exec -i -e PGOPTIONS='-c lock_timeout=30s' \
--     -e NAMELEAF_ADMIN_PASSWORD -e NAMELEAF_APP_PASSWORD <container> \
--     psql -X -v ON_ERROR_STOP=1 -U postgres -d postgres < roles.sql

\set ON_ERROR_STOP on
SET lock_timeout = '30s';

\getenv admin_password NAMELEAF_ADMIN_PASSWORD
\getenv app_password NAMELEAF_APP_PASSWORD

-- nameleaf_admin -------------------------------------------------------------
SELECT NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nameleaf_admin')
    AS need_admin \gset
\if :need_admin
  \if :{?admin_password}
    CREATE ROLE nameleaf_admin LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
        NOREPLICATION NOBYPASSRLS PASSWORD :'admin_password';
    \echo 'created role nameleaf_admin'
  \else
    DO $$ BEGIN RAISE EXCEPTION
      'NAMELEAF_ADMIN_PASSWORD is required to create nameleaf_admin'; END $$;
  \endif
\else
  ALTER ROLE nameleaf_admin LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
      NOREPLICATION NOBYPASSRLS;
  \if :{?admin_password}
    ALTER ROLE nameleaf_admin PASSWORD :'admin_password';
    \echo 'nameleaf_admin exists; password rotated'
  \else
    \echo 'nameleaf_admin exists; password unchanged'
  \endif
\endif

-- nameleaf_app ---------------------------------------------------------------
SELECT NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nameleaf_app')
    AS need_app \gset
\if :need_app
  \if :{?app_password}
    CREATE ROLE nameleaf_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
        NOREPLICATION NOBYPASSRLS NOINHERIT PASSWORD :'app_password';
    \echo 'created role nameleaf_app'
  \else
    DO $$ BEGIN RAISE EXCEPTION
      'NAMELEAF_APP_PASSWORD is required to create nameleaf_app'; END $$;
  \endif
\else
  ALTER ROLE nameleaf_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
      NOREPLICATION NOBYPASSRLS NOINHERIT;
  \if :{?app_password}
    ALTER ROLE nameleaf_app PASSWORD :'app_password';
    \echo 'nameleaf_app exists; password rotated'
  \else
    \echo 'nameleaf_app exists; password unchanged'
  \endif
\endif

SELECT rolname, rolsuper, rolcreatedb, rolcreaterole, rolcanlogin
  FROM pg_roles WHERE rolname IN ('nameleaf_admin', 'nameleaf_app')
 ORDER BY 1;
