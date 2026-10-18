#!/usr/bin/env bash
# restore_reference.sh -- restore a slimmed reference build into a NEW
# database (PARTITION C4; first run in C5). See README.md.
#
# THE PUBLISH MECHANISM (Findings C-4.x; C6 turns this into the runbook):
#   1. build_reference.sh on the Mac -> build dir (reference_public.dump,
#      manifest.txt, live_schema.sql, ...). Transfer the build dir.
#   2. roles.sql once per Postgres cluster (re-runnable).
#   3. THIS SCRIPT, into a new database name_generator_<stamp> in the same
#      Postgres instance, while the current database keeps serving:
#        preflight -> CREATE DATABASE (owner nameleaf_admin) ->
#        CREATE EXTENSION vector -> pg_restore -j (owner nameleaf_admin) ->
#        ANALYZE -> live (empty, or carried from the current database) ->
#        grants.sql -> check_restore.sh -> check_roles.sql as nameleaf_app
#   4. (not scripted here) switch the backend's DATABASE_URL to
#      nameleaf_app@.../<new db> and restart it. Keep the previous database
#      for one publish cycle as the rollback; drop it only after the next
#      publish succeeds.
#   `live` and `public` share one database (the dropdown joins
#   live.sense_selection_stats to senses). Carrying `live` needs no writes in
#   flight: true while RANKING_STATS_WRITE=0; otherwise stop the backend for
#   the copy. The script refuses a source with other connections unless
#   --allow-connections is given.
#
# This script never drops a database. If it fails part-way, the new database
# is left as it is for inspection; drop it by hand when done.
#
# Usage (targets required, no defaults):
#   restore_reference.sh --container <ctr> --db name_generator_YYYYMMDD \
#       --build-dir <dir> --live empty
#   restore_reference.sh ... --live carry-from <current db> [--allow-connections]
#   options: --jobs 2  --maintenance-work-mem 1GB  --parallel-workers 1
#   (Parallel HNSW builds need shared memory >= maintenance_work_mem; the
#    production compose gives Postgres shm_size 1gb, hence the 1GB default.)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "$SCRIPT_DIR/lib.sh"

CTR="" DB="" BUILD_DIR="" LIVE_MODE="" CARRY_FROM="" ALLOW_CONN=0
JOBS="2" MWM="1GB" WORKERS="1"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --container) CTR="${2:-}"; shift 2 ;;
        --db) DB="${2:-}"; shift 2 ;;
        --build-dir) BUILD_DIR="${2:-}"; shift 2 ;;
        --live)
            LIVE_MODE="${2:-}"
            if [[ "$LIVE_MODE" == carry-from ]]; then
                [[ $# -ge 3 ]] || die "--live carry-from needs a source database"
                CARRY_FROM="$3"; shift 3
            else
                [[ $# -ge 2 ]] || die "--live needs 'empty' or 'carry-from <db>'"
                shift 2
            fi ;;
        --allow-connections) ALLOW_CONN=1; shift ;;
        --jobs) JOBS="${2:-}"; shift 2 ;;
        --maintenance-work-mem) MWM="${2:-}"; shift 2 ;;
        --parallel-workers) WORKERS="${2:-}"; shift 2 ;;
        *) die "unknown argument: $1" ;;
    esac
done

# --- argument checks: before ANY binary is invoked ---------------------------
require_arg --container "$CTR"
require_arg --db "$DB"
require_arg --build-dir "$BUILD_DIR"
require_arg --live "$LIVE_MODE"
refuse_target "$CTR" "$DB"
[[ "$CTR" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$ ]] || die "bad --container"
valid_identifier --db "$DB"
case "$LIVE_MODE" in
    empty) ;;
    carry-from)
        require_arg "--live carry-from <db>" "$CARRY_FROM"
        valid_identifier "--live carry-from" "$CARRY_FROM"
        [[ "$CARRY_FROM" != "$DB" ]] || die "cannot carry live from the target itself" ;;
    *) die "--live must be 'empty' or 'carry-from <db>'" ;;
esac
[[ "$JOBS" =~ ^[0-9]+$ && "$WORKERS" =~ ^[0-9]+$ ]] || die "bad --jobs/--parallel-workers"
[[ "$MWM" =~ ^[0-9]+(kB|MB|GB)$ ]] || die "bad --maintenance-work-mem"
[[ -f "$BUILD_DIR/manifest.txt" ]] || die "no manifest.txt in $BUILD_DIR"
LOG_DIR="$BUILD_DIR/restore_$DB"
refuse_existing_path "$LOG_DIR"

mkdir "$LOG_DIR"
exec > >(tee -a "$LOG_DIR/restore.log") 2>&1
note "restore_reference.sh start: container=$CTR db=$DB live=$LIVE_MODE ${CARRY_FROM}"

manifest() { manifest_value "$BUILD_DIR" "$1"; }
DUMP="$BUILD_DIR/$(manifest reference_dump)"
LIVE_SQL="$BUILD_DIR/$(manifest live_schema_file)"
TMP_DUMP="/tmp/nameleaf_restore_$DB.dump"
TMP_TOC="/tmp/nameleaf_restore_$DB.toc"
STEP="preflight"
trap 'note "FAILED during step: $STEP. Database $DB (if created) is left as is for inspection; nothing was dropped."' ERR

T0=$SECONDS
printf 'step\tseconds\n' > "$LOG_DIR/timings.tsv"
step_done() { printf '%s\t%s\n' "$1" "$((SECONDS - T0))" >> "$LOG_DIR/timings.tsv"; T0=$SECONDS; note "step $1 done"; }

# --- preflight ------------------------------------------------------------------
note "preflight"
assert_not_master_container "$CTR"
assert_shm_fits "$CTR" "$MWM" "$WORKERS"
[[ "$(sha256_of "$DUMP")" == "$(manifest reference_dump_sha256)" ]] \
    || die "sha256 of $DUMP does not match the manifest"
VERSION_NUM=$(psql_ro "$CTR" postgres -At -c "SHOW server_version_num")
[[ "$VERSION_NUM" == 17* ]] || die "target is not Postgres 17 ($VERSION_NUM)"
ROLES=$(psql_ro "$CTR" postgres -At -c \
    "SELECT count(*) FROM pg_roles WHERE rolname IN ('nameleaf_admin', 'nameleaf_app')")
[[ "$ROLES" == 2 ]] || die "roles missing: run roles.sql first"
EXISTS=$(psql_ro "$CTR" postgres -At -c "SELECT count(*) FROM pg_database WHERE datname = '$DB'")
[[ "$EXISTS" == 0 ]] || die "database $DB already exists"
if [[ "$LIVE_MODE" == carry-from ]]; then
    SRC_EXISTS=$(psql_ro "$CTR" postgres -At -c "SELECT count(*) FROM pg_database WHERE datname = '$CARRY_FROM'")
    [[ "$SRC_EXISTS" == 1 ]] || die "source database $CARRY_FROM not found"
    SRC_ALEMBIC=$(psql_ro "$CTR" "$CARRY_FROM" -At -c "SELECT version_num FROM public.alembic_version")
    [[ "$SRC_ALEMBIC" == "$(manifest alembic)" ]] \
        || die "source $CARRY_FROM is at alembic $SRC_ALEMBIC, the build at $(manifest alembic): live schemas may differ"
    SRC_LIVE=$(psql_ro "$CTR" "$CARRY_FROM" -At -c \
        "SELECT count(*) FROM pg_tables WHERE schemaname = 'live' AND tablename IN
           ('sense_selection_stats','sense_selection_events','word_search_stats','word_search_events')")
    [[ "$SRC_LIVE" == 4 ]] || die "source $CARRY_FROM lacks the four live tables"
    SRC_CONN=$(psql_ro "$CTR" postgres -At -c \
        "SELECT count(*) FROM pg_stat_activity WHERE backend_type = 'client backend'
           AND datname = '$CARRY_FROM' AND pid <> pg_backend_pid()")
    if [[ "$SRC_CONN" != 0 && "$ALLOW_CONN" == 0 ]]; then
        die "$SRC_CONN connection(s) to $CARRY_FROM: stop the backend, or pass --allow-connections if RANKING_STATS_WRITE=0"
    fi
    note "carrying live from $CARRY_FROM (alembic $SRC_ALEMBIC, $SRC_CONN other connection(s))"
fi
docker exec "$CTR" test ! -e "$TMP_DUMP" || die "$TMP_DUMP already exists in $CTR"
step_done preflight

# --- create database + extension ------------------------------------------------
STEP="create"
psql_rw "$CTR" postgres -c "CREATE DATABASE $DB OWNER nameleaf_admin"
psql_rw "$CTR" "$DB" -c "CREATE EXTENSION IF NOT EXISTS vector"
PGVECTOR=$(psql_rw "$CTR" "$DB" -At -c "SELECT extversion FROM pg_extension WHERE extname = 'vector'")
[[ "$PGVECTOR" == "$(manifest pgvector_master)" ]] \
    || note "WARNING: pgvector here is $PGVECTOR, master's was $(manifest pgvector_master)"
step_done create

# --- restore public ---------------------------------------------------------------
STEP="restore"
docker cp "$DUMP" "$CTR:$TMP_DUMP"
docker exec -e PGOPTIONS="$PGOPTS_WRITE" "$CTR" pg_restore -l "$TMP_DUMP" > "$LOG_DIR/toc.list"
filter_toc "$LOG_DIR/toc.list" "$LOG_DIR/toc.filtered"
docker cp "$LOG_DIR/toc.filtered" "$CTR:$TMP_TOC"
note "pg_restore -j $JOBS, maintenance_work_mem=$MWM, max_parallel_maintenance_workers=$WORKERS"
docker exec -e PGOPTIONS="$PGOPTS_WRITE -c maintenance_work_mem=$MWM -c max_parallel_maintenance_workers=$WORKERS" \
    "$CTR" pg_restore -U postgres -d "$DB" -L "$TMP_TOC" --no-owner --no-privileges \
    --role=nameleaf_admin --exit-on-error -j "$JOBS" "$TMP_DUMP"
docker exec "$CTR" rm -f "$TMP_DUMP" "$TMP_TOC"
step_done restore

# --- planner statistics (pg_restore does not ANALYZE) ------------------------------
STEP="analyze"
docker exec -e PGOPTIONS="$PGOPTS_WRITE" "$CTR" \
    vacuumdb -U postgres -d "$DB" --analyze-only -j "$JOBS"
step_done analyze

# --- live ---------------------------------------------------------------------------
STEP="live"
psql_rw "$CTR" "$DB" -c "SET ROLE nameleaf_admin" -f - < "$LIVE_SQL"
if [[ "$LIVE_MODE" == carry-from ]]; then
    docker exec -e PGOPTIONS="$PGOPTS_READONLY" "$CTR" \
        pg_dump -U postgres -d "$CARRY_FROM" -n live --data-only -Fc --lock-wait-timeout=30s \
      | docker exec -i -e PGOPTIONS="$PGOPTS_WRITE" "$CTR" \
        pg_restore -U postgres -d "$DB" --data-only --no-owner --no-privileges \
        --role=nameleaf_admin --exit-on-error
    for t in sense_selection_stats sense_selection_events word_search_stats word_search_events; do
        a=$(psql_ro "$CTR" "$CARRY_FROM" -At -c "SELECT count(*) FROM live.$t")
        b=$(psql_ro "$CTR" "$DB" -At -c "SELECT count(*) FROM live.$t")
        [[ "$a" == "$b" ]] || die "live.$t: $a rows in $CARRY_FROM, $b in $DB"
        note "live.$t carried: $b rows"
    done
fi
step_done live

# --- grants -------------------------------------------------------------------------
STEP="grants"
psql_rw "$CTR" "$DB" -v db="$DB" -f - < "$SCRIPT_DIR/grants.sql"
step_done grants

# --- checks -------------------------------------------------------------------------
STEP="check_restore"
"$SCRIPT_DIR/check_restore.sh" --container "$CTR" --db "$DB" --build-dir "$BUILD_DIR"
step_done check_restore

STEP="check_roles"
docker exec -i -e PGOPTIONS="$PGOPTS_WRITE" "$CTR" \
    psql -X -v ON_ERROR_STOP=1 -U nameleaf_app -d "$DB" < "$SCRIPT_DIR/check_roles.sql" \
    2>&1 | tee "$LOG_DIR/check_roles.txt"
grep -q 'FAIL' "$LOG_DIR/check_roles.txt" && die "check_roles reported FAIL"
[[ "$(grep -c 'PASS' "$LOG_DIR/check_roles.txt")" == 10 ]] || die "check_roles did not report 10 PASS lines"
step_done check_roles

note "restore complete: database $DB is ready. Next (by hand): point DATABASE_URL at"
note "  postgresql+psycopg://nameleaf_app:<password>@db:5432/$DB , restart the backend,"
note "  and keep the previous database for one publish cycle as the rollback."
