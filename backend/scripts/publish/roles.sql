-- roles.sql -- least-privilege roles (PARTITION C4, Stage 2d).
-- Cluster-wide; run ONCE per Postgres cluster, re-runnable (idempotent).
-- Per-database grants are in grants.sql, run for every new database.
--
--   nameforge_admin  LOGIN; owns every restored object (the restore runs
--                    pg_restore --no-owner --role=nameforge_admin, and the
--                    database is created OWNER nameforge_admin).
--   nameforge_app    LOGIN; what the backend connects as (grants.sql gives
--                    it read-only public, SELECT/INSERT/UPDATE on live).
--
-- Passwords are NEVER in this file or the repo. They are read from the
-- environment of the psql process with \getenv; pass them into the
-- container with `docker exec -e NAMEFORGE_ADMIN_PASSWORD -e
-- NAMEFORGE_APP_PASSWORD ...`, which copies the host values without putting
-- them on any command line. A role that does not exist yet needs its
-- password; an existing role's password is rotated only if one is given.
--
-- Run as the superuser, e.g. (see README.md):
--   docker exec -i -e PGOPTIONS='-c lock_timeout=30s' \
--     -e NAMEFORGE_ADMIN_PASSWORD -e NAMEFORGE_APP_PASSWORD <container> \
--     psql -X -v ON_ERROR_STOP=1 -U postgres -d postgres < roles.sql

\set ON_ERROR_STOP on
SET lock_timeout = '30s';

\getenv admin_password NAMEFORGE_ADMIN_PASSWORD
\getenv app_password NAMEFORGE_APP_PASSWORD

-- nameforge_admin -------------------------------------------------------------
SELECT NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nameforge_admin')
    AS need_admin \gset
\if :need_admin
  \if :{?admin_password}
    CREATE ROLE nameforge_admin LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
        NOREPLICATION NOBYPASSRLS PASSWORD :'admin_password';
    \echo 'created role nameforge_admin'
  \else
    DO $$ BEGIN RAISE EXCEPTION
      'NAMEFORGE_ADMIN_PASSWORD is required to create nameforge_admin'; END $$;
  \endif
\else
  ALTER ROLE nameforge_admin LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
      NOREPLICATION NOBYPASSRLS;
  \if :{?admin_password}
    ALTER ROLE nameforge_admin PASSWORD :'admin_password';
    \echo 'nameforge_admin exists; password rotated'
  \else
    \echo 'nameforge_admin exists; password unchanged'
  \endif
\endif

-- nameforge_app ---------------------------------------------------------------
SELECT NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nameforge_app')
    AS need_app \gset
\if :need_app
  \if :{?app_password}
    CREATE ROLE nameforge_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
        NOREPLICATION NOBYPASSRLS NOINHERIT PASSWORD :'app_password';
    \echo 'created role nameforge_app'
  \else
    DO $$ BEGIN RAISE EXCEPTION
      'NAMEFORGE_APP_PASSWORD is required to create nameforge_app'; END $$;
  \endif
\else
  ALTER ROLE nameforge_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
      NOREPLICATION NOBYPASSRLS NOINHERIT;
  \if :{?app_password}
    ALTER ROLE nameforge_app PASSWORD :'app_password';
    \echo 'nameforge_app exists; password rotated'
  \else
    \echo 'nameforge_app exists; password unchanged'
  \endif
\endif

SELECT rolname, rolsuper, rolcreatedb, rolcreaterole, rolcanlogin
  FROM pg_roles WHERE rolname IN ('nameforge_admin', 'nameforge_app')
 ORDER BY 1;
