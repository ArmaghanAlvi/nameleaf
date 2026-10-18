-- check_roles.sql -- verify nameleaf_app's privileges (PARTITION C4, Stage 2d).
-- Run AS nameleaf_app, connected to the published database:
--   docker exec -i -e PGOPTIONS='-c lock_timeout=30s' <container> \
--     psql -X -U nameleaf_app -d <db> < check_roles.sql
-- Prints one NOTICE per item: "PASS <item>" or "FAIL <item>". Everything
-- runs inside ONE transaction that is rolled back, so nothing persists even
-- if a check that should fail unexpectedly succeeds. It refuses to run as
-- any other role (a superuser would "succeed" at everything).

\set ON_ERROR_STOP on
BEGIN;
SET LOCAL lock_timeout = '30s';

DO $$ BEGIN
  IF current_user <> 'nameleaf_app' THEN
    RAISE EXCEPTION 'run check_roles.sql as nameleaf_app (current_user=%)', current_user;
  END IF;
END $$;

-- Must be REFUSED ---------------------------------------------------------------
DO $$ BEGIN
  BEGIN
    EXECUTE 'DROP TABLE public.senses';
    RAISE NOTICE 'FAIL drop_public_table: DROP TABLE public.senses succeeded';
  EXCEPTION
    WHEN insufficient_privilege THEN RAISE NOTICE 'PASS drop_public_table: refused (%)', SQLERRM;
    WHEN OTHERS THEN RAISE NOTICE 'FAIL drop_public_table: unexpected % %', SQLSTATE, SQLERRM;
  END;
END $$;

DO $$ BEGIN
  BEGIN
    EXECUTE 'INSERT INTO public.alembic_version (version_num) VALUES (''check_roles'')';
    RAISE NOTICE 'FAIL insert_public: INSERT into public succeeded';
  EXCEPTION
    WHEN insufficient_privilege THEN RAISE NOTICE 'PASS insert_public: refused (%)', SQLERRM;
    WHEN OTHERS THEN RAISE NOTICE 'FAIL insert_public: unexpected % %', SQLSTATE, SQLERRM;
  END;
END $$;

DO $$ BEGIN
  BEGIN
    EXECUTE 'UPDATE public.alembic_version SET version_num = version_num';
    RAISE NOTICE 'FAIL update_public: UPDATE on public succeeded';
  EXCEPTION
    WHEN insufficient_privilege THEN RAISE NOTICE 'PASS update_public: refused (%)', SQLERRM;
    WHEN OTHERS THEN RAISE NOTICE 'FAIL update_public: unexpected % %', SQLSTATE, SQLERRM;
  END;
END $$;

DO $$ BEGIN
  BEGIN
    EXECUTE 'CREATE TABLE public.nameleaf_check_roles (x int)';
    RAISE NOTICE 'FAIL create_in_public: CREATE TABLE in public succeeded';
  EXCEPTION
    WHEN insufficient_privilege THEN RAISE NOTICE 'PASS create_in_public: refused (%)', SQLERRM;
    WHEN OTHERS THEN RAISE NOTICE 'FAIL create_in_public: unexpected % %', SQLSTATE, SQLERRM;
  END;
END $$;

DO $$ BEGIN
  BEGIN
    EXECUTE 'DELETE FROM live.sense_selection_events WHERE sense_id = -1';
    RAISE NOTICE 'FAIL delete_live: DELETE on live succeeded';
  EXCEPTION
    WHEN insufficient_privilege THEN RAISE NOTICE 'PASS delete_live: refused (%)', SQLERRM;
    WHEN OTHERS THEN RAISE NOTICE 'FAIL delete_live: unexpected % %', SQLSTATE, SQLERRM;
  END;
END $$;

DO $$ BEGIN
  BEGIN
    EXECUTE 'TRUNCATE live.sense_selection_events';
    RAISE NOTICE 'FAIL truncate_live: TRUNCATE on live succeeded';
  EXCEPTION
    WHEN insufficient_privilege THEN RAISE NOTICE 'PASS truncate_live: refused (%)', SQLERRM;
    WHEN OTHERS THEN RAISE NOTICE 'FAIL truncate_live: unexpected % %', SQLSTATE, SQLERRM;
  END;
END $$;

-- Must WORK ---------------------------------------------------------------------
DO $$ BEGIN
  BEGIN
    PERFORM 1 FROM public.senses LIMIT 1;
    PERFORM 1 FROM public.sense_embeddings LIMIT 1;
    RAISE NOTICE 'PASS select_public: SELECT on public works';
  EXCEPTION
    WHEN OTHERS THEN RAISE NOTICE 'FAIL select_public: % %', SQLSTATE, SQLERRM;
  END;
END $$;

DO $$ BEGIN
  BEGIN
    PERFORM 1 FROM live.sense_selection_stats LIMIT 1;
    RAISE NOTICE 'PASS select_live: SELECT on live works';
  EXCEPTION
    WHEN OTHERS THEN RAISE NOTICE 'FAIL select_live: % %', SQLSTATE, SQLERRM;
  END;
END $$;

DO $$ BEGIN
  BEGIN
    -- Uses the owned sequence (nextval), as record_sense_selection does.
    INSERT INTO live.sense_selection_events (sense_id, query_text)
        VALUES (-1, 'check_roles');
    RAISE NOTICE 'PASS insert_live: INSERT into live (with sequence) works';
  EXCEPTION
    WHEN OTHERS THEN RAISE NOTICE 'FAIL insert_live: % %', SQLSTATE, SQLERRM;
  END;
END $$;

DO $$ BEGIN
  BEGIN
    INSERT INTO live.sense_selection_stats (sense_id, selection_count)
        VALUES (-1, 0);
    UPDATE live.sense_selection_stats
       SET selection_count = selection_count + 1 WHERE sense_id = -1;
    RAISE NOTICE 'PASS update_live: INSERT/UPDATE on live works';
  EXCEPTION
    WHEN OTHERS THEN RAISE NOTICE 'FAIL update_live: % %', SQLSTATE, SQLERRM;
  END;
END $$;

ROLLBACK;
