#!/usr/bin/env bash
# build_reference.sh -- build the slimmed reference dump of schema `public`
# from master (PARTITION C4; first run in C5). See README.md.
#
# MASTER IS ONLY READ. It is dumped read-only (pg_dump inside the master
# container, written to a container path, docker cp'd out, then removed --
# the C1 method: a custom-format archive written to a pipe has no data
# offsets, which parallel restore needs), and queried in sessions with
# default_transaction_read_only=on. All writing happens in a THROWAWAY build
# container started here (docker run --rm, no named volume, the output
# directory bind-mounted at /work).
#
# MASTER MUST BE QUIET (no dev server, no searches) for the whole build. With
# the query-time LLM trickle unfenced, an ordinary local search can write
# public.root_llm_attempts and public.sense_translations, so C1's row counts
# are NOT the reference -- this build's manifest is.
#
# Steps (--stop-after NAME stops after a step and leaves the container up):
#   preflight      master read-only checks; refuse if not quiet, not at the
#                  required alembic revision, not Postgres 17, or more than
#                  one HNSW index on sense_embeddings
#   dump_master    pg_dump -Fc -n public (read-only) -> master_public.dump
#   start          start the build container; CREATE DATABASE build;
#                  CREATE EXTENSION vector
#   predata        restore pre-data (tables, no indexes)
#   data           restore data (-j)
#   project        projection.sql: keep only the synonyms keys of raw_sense /
#                  raw_entry, empty embedded_text -- before any index exists
#   postdata       restore post-data (indexes incl. HNSW, constraints)
#   digests        counts + synonyms + embeddings digests, master vs build;
#                  any difference fails the build
#   dump_slim      pg_dump -Fc -n public of the build -> reference_public.dump
#   live_schema    pg_dump -n live --schema-only from master -> live_schema.sql
#   manifest       manifest.txt + TSVs
#
# Usage (all four required; no defaults for targets):
#   build_reference.sh --source-container name_generator_db \
#       --source-db name_generator \
#       --build-container nameforge_build --port 55432 \
#       --out ../nameforge_build_YYYYMMDD
#   options: --image pgvector/pgvector:pg17  --shm-size 3g  --jobs 4
#            --maintenance-work-mem 2GB  --parallel-workers 2
#            --stop-after STEP  --keep-container
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "$SCRIPT_DIR/lib.sh"

STEPS="preflight dump_master start predata data project postdata digests dump_slim live_schema manifest"

SRC_CTR="" SRC_DB="" BUILD_CTR="" PORT="" OUT=""
IMAGE="pgvector/pgvector:pg17" SHM="3g" JOBS="4" MWM="2GB" WORKERS="2"
STOP_AFTER="" KEEP=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --source-container) SRC_CTR="${2:-}"; shift 2 ;;
        --source-db) SRC_DB="${2:-}"; shift 2 ;;
        --build-container) BUILD_CTR="${2:-}"; shift 2 ;;
        --port) PORT="${2:-}"; shift 2 ;;
        --out) OUT="${2:-}"; shift 2 ;;
        --image) IMAGE="${2:-}"; shift 2 ;;
        --shm-size) SHM="${2:-}"; shift 2 ;;
        --jobs) JOBS="${2:-}"; shift 2 ;;
        --maintenance-work-mem) MWM="${2:-}"; shift 2 ;;
        --parallel-workers) WORKERS="${2:-}"; shift 2 ;;
        --stop-after) STOP_AFTER="${2:-}"; shift 2 ;;
        --keep-container) KEEP=1; shift ;;
        *) die "unknown argument: $1" ;;
    esac
done

# --- argument checks: before ANY binary is invoked ---------------------------
require_arg --source-container "$SRC_CTR"
require_arg --source-db "$SRC_DB"
require_arg --build-container "$BUILD_CTR"
require_arg --port "$PORT"
require_arg --out "$OUT"
refuse_target "$BUILD_CTR" "" "$PORT"
[[ "$BUILD_CTR" != "$SRC_CTR" ]] || die "build container must differ from the source"
[[ "$BUILD_CTR" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$ ]] || die "bad --build-container"
if ! [[ "$PORT" =~ ^[0-9]+$ ]] || (( PORT < 1024 || PORT > 65535 )); then
    die "--port must be 1024-65535"
fi
valid_identifier --source-db "$SRC_DB"
[[ "$JOBS" =~ ^[0-9]+$ && "$WORKERS" =~ ^[0-9]+$ ]] || die "bad --jobs/--parallel-workers"
[[ "$MWM" =~ ^[0-9]+(kB|MB|GB)$ ]] || die "bad --maintenance-work-mem"
if [[ -n "$STOP_AFTER" && " $STEPS " != *" $STOP_AFTER "* ]]; then
    die "--stop-after must be one of: $STEPS"
fi
refuse_existing_path "$OUT"

mkdir "$OUT"
OUT_ABS="$(cd "$OUT" && pwd)"
exec > >(tee -a "$OUT_ABS/build.log") 2>&1
note "build_reference.sh start; out=$OUT_ABS"

STARTED=0
cleanup() {
    if [[ "$STARTED" == 1 && "$KEEP" == 0 ]]; then
        note "stopping build container $BUILD_CTR (--rm removes it)"
        docker stop "$BUILD_CTR" >/dev/null || true
    elif [[ "$STARTED" == 1 ]]; then
        note "build container $BUILD_CTR left running; stop it with: docker stop $BUILD_CTR"
    fi
}
trap cleanup EXIT

T0=$SECONDS
printf 'step\tseconds\n' > "$OUT_ABS/timings.tsv"
step_done() {  # name
    printf '%s\t%s\n' "$1" "$((SECONDS - T0))" >> "$OUT_ABS/timings.tsv"
    T0=$SECONDS
    note "step $1 done"
    if [[ "$STOP_AFTER" == "$1" ]]; then
        KEEP=1
        note "--stop-after $1: stopping here"
        exit 0
    fi
}

TAB=$'\t'
SRC_TMP="/tmp/nameforge_build_master_public.dump"

# --- preflight (master read-only) --------------------------------------------
note "preflight"
docker inspect "$BUILD_CTR" >/dev/null 2>&1 && die "container $BUILD_CTR already exists"
if docker ps --format '{{.Ports}}' | grep -q ":$PORT->"; then
    die "port $PORT is already published by a container"
fi
SRC_VERSION_NUM=$(psql_ro "$SRC_CTR" "$SRC_DB" -At -c "SHOW server_version_num")
[[ "$SRC_VERSION_NUM" == 17* ]] || die "source is not Postgres 17 ($SRC_VERSION_NUM)"
SRC_ALEMBIC=$(psql_ro "$SRC_CTR" "$SRC_DB" -At -c "SELECT version_num FROM public.alembic_version")
[[ "$SRC_ALEMBIC" == "$REQUIRED_ALEMBIC" ]] \
    || die "source alembic is $SRC_ALEMBIC, expected $REQUIRED_ALEMBIC"
OTHERS=$(psql_ro "$SRC_CTR" "$SRC_DB" -At -c \
    "SELECT count(*) FROM pg_stat_activity WHERE backend_type = 'client backend'
       AND datname = current_database() AND pid <> pg_backend_pid()")
[[ "$OTHERS" == 0 ]] || die "master is not quiet: $OTHERS other client connection(s); stop the dev server"
FINGERPRINT_BEFORE=$(psql_ro "$SRC_CTR" "$SRC_DB" -At -c \
    "SELECT count(*) || '|' || coalesce(sum(selection_count), 0) || '|' || coalesce(max(last_selected_at)::text, '')
       FROM live.sense_selection_stats")
SRC_PGVECTOR=$(psql_ro "$SRC_CTR" "$SRC_DB" -At -c \
    "SELECT extversion FROM pg_extension WHERE extname = 'vector'")
psql_ro "$SRC_CTR" "$SRC_DB" -At -F "$TAB" -c \
    "SELECT c.relname, a.amname, pg_size_pretty(pg_relation_size(c.oid)), i.indisvalid
       FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid
       JOIN pg_am a ON a.oid = c.relam
      WHERE i.indrelid = 'public.sense_embeddings'::regclass ORDER BY 1" \
    > "$OUT_ABS/master_sense_embeddings_indexes.tsv"
cat "$OUT_ABS/master_sense_embeddings_indexes.tsv"
HNSW_COUNT=$(awk -F "$TAB" '$2 == "hnsw"' "$OUT_ABS/master_sense_embeddings_indexes.tsv" | wc -l | tr -d ' ')
[[ "$HNSW_COUNT" == 1 ]] \
    || die "expected exactly 1 HNSW index on sense_embeddings, found $HNSW_COUNT (each extra one doubles every restore)"
SOURCE_TIME="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
GIT_HEAD="$(git -C "$SCRIPT_DIR" rev-parse HEAD)"
GIT_DIRTY="$(git -C "$SCRIPT_DIR" status --porcelain | wc -l | tr -d ' ')"
note "source pg $SRC_VERSION_NUM, alembic $SRC_ALEMBIC, pgvector $SRC_PGVECTOR, fingerprint $FINGERPRINT_BEFORE, git $GIT_HEAD (dirty files: $GIT_DIRTY)"
step_done preflight

# --- dump master's public schema (read-only) ---------------------------------
note "dump_master"
docker exec "$SRC_CTR" test ! -e "$SRC_TMP" || die "$SRC_TMP already exists inside $SRC_CTR"
docker exec -e PGOPTIONS="$PGOPTS_READONLY" "$SRC_CTR" \
    pg_dump -U postgres -d "$SRC_DB" -n public -Fc --lock-wait-timeout=30s -f "$SRC_TMP"
docker cp "$SRC_CTR:$SRC_TMP" "$OUT_ABS/master_public.dump"
docker exec "$SRC_CTR" rm -f "$SRC_TMP"
PG_DUMP_VERSION="$(docker exec -e PGOPTIONS="$PGOPTS_READONLY" "$SRC_CTR" pg_dump --version)"
MASTER_DUMP_SHA="$(sha256_of "$OUT_ABS/master_public.dump")"
MASTER_DUMP_BYTES="$(wc -c < "$OUT_ABS/master_public.dump" | tr -d ' ')"
note "master_public.dump $MASTER_DUMP_BYTES bytes sha256 $MASTER_DUMP_SHA ($PG_DUMP_VERSION)"
step_done dump_master

# --- start the throwaway build container -------------------------------------
note "start"
BUILD_PW="$(LC_ALL=C tr -dc 'a-f0-9' < /dev/urandom | head -c 32)"
docker run -d --rm --name "$BUILD_CTR" --shm-size "$SHM" \
    -e POSTGRES_PASSWORD="$BUILD_PW" -p "127.0.0.1:$PORT:5432" \
    -v "$OUT_ABS:/work" "$IMAGE" >/dev/null
STARTED=1
assert_not_master_container "$BUILD_CTR"
assert_shm_fits "$BUILD_CTR" "$MWM" "$WORKERS"
for _ in $(seq 1 90); do
    if docker logs "$BUILD_CTR" 2>&1 | grep -q 'PostgreSQL init process complete' \
       && docker exec "$BUILD_CTR" pg_isready -U postgres -q; then
        break
    fi
    sleep 2
done
docker exec "$BUILD_CTR" pg_isready -U postgres -q || die "build container did not become ready"
BUILD_IMAGE_ID="$(docker inspect -f '{{.Image}}' "$BUILD_CTR")"
psql_rw "$BUILD_CTR" postgres -c "CREATE DATABASE build"
psql_rw "$BUILD_CTR" build -c "CREATE EXTENSION IF NOT EXISTS vector"
BUILD_PGVECTOR=$(psql_rw "$BUILD_CTR" build -At -c \
    "SELECT extversion FROM pg_extension WHERE extname = 'vector'")
[[ "$BUILD_PGVECTOR" == "$SRC_PGVECTOR" ]] \
    || note "WARNING: pgvector differs: master $SRC_PGVECTOR, build $BUILD_PGVECTOR"
step_done start

# --- restore pre-data, data ---------------------------------------------------
note "predata"
docker exec -e PGOPTIONS="$PGOPTS_WRITE" "$BUILD_CTR" \
    pg_restore -l /work/master_public.dump > "$OUT_ABS/master_toc.list"
filter_toc "$OUT_ABS/master_toc.list" "$OUT_ABS/master_toc.filtered"
docker exec -e PGOPTIONS="$PGOPTS_WRITE" "$BUILD_CTR" \
    pg_restore -U postgres -d build --section=pre-data -L /work/master_toc.filtered \
    --no-owner --no-privileges --exit-on-error /work/master_public.dump
step_done predata

note "data"
docker exec -e PGOPTIONS="$PGOPTS_WRITE" "$BUILD_CTR" \
    pg_restore -U postgres -d build --section=data -L /work/master_toc.filtered \
    --no-owner --no-privileges --exit-on-error -j "$JOBS" /work/master_public.dump
step_done data

# --- project (no indexes exist yet) ------------------------------------------
note "project"
psql_rw "$BUILD_CTR" build < "$SCRIPT_DIR/projection.sql" | tee "$OUT_ABS/projection.out"
step_done project

# --- post-data (indexes, constraints; HNSW build) ----------------------------
note "postdata (maintenance_work_mem=$MWM, max_parallel_maintenance_workers=$WORKERS, jobs=$JOBS)"
docker exec -e PGOPTIONS="$PGOPTS_WRITE -c maintenance_work_mem=$MWM -c max_parallel_maintenance_workers=$WORKERS" \
    "$BUILD_CTR" pg_restore -U postgres -d build --section=post-data \
    -L /work/master_toc.filtered --no-owner --no-privileges --exit-on-error \
    -j "$JOBS" /work/master_public.dump
step_done postdata

# --- digests and counts: master vs build --------------------------------------
note "digests"
for side in master build; do
    if [[ "$side" == master ]]; then ctr="$SRC_CTR" db="$SRC_DB"; else ctr="$BUILD_CTR" db="build"; fi
    psql_ro "$ctr" "$db" -At -F "$TAB" < "$SCRIPT_DIR/synonyms_digest.sql" > "$OUT_ABS/${side}_digests.tsv"
    psql_ro "$ctr" "$db" -At -F "$TAB" < "$SCRIPT_DIR/embeddings_digest.sql" >> "$OUT_ABS/${side}_digests.tsv"
    psql_ro "$ctr" "$db" -At -F "$TAB" < "$SCRIPT_DIR/public_counts.sql" > "$OUT_ABS/${side}_counts.tsv"
done
cat "$OUT_ABS/master_digests.tsv"
diff "$OUT_ABS/master_digests.tsv" "$OUT_ABS/build_digests.tsv" \
    || die "digests differ between master and build (see above); the projection is wrong"
diff "$OUT_ABS/master_counts.tsv" "$OUT_ABS/build_counts.tsv" \
    || die "row counts differ between master and build"
note "digests and counts identical (master vs build)"
step_done digests

# --- slim dump ----------------------------------------------------------------
note "dump_slim"
docker exec -e PGOPTIONS="$PGOPTS_READONLY" "$BUILD_CTR" \
    pg_dump -U postgres -d build -n public -Fc --lock-wait-timeout=30s \
    -f /work/reference_public.dump
SLIM_SHA="$(sha256_of "$OUT_ABS/reference_public.dump")"
SLIM_BYTES="$(wc -c < "$OUT_ABS/reference_public.dump" | tr -d ' ')"
note "reference_public.dump $SLIM_BYTES bytes sha256 $SLIM_SHA"
step_done dump_slim

# --- live schema from master (schema only, read-only) -------------------------
note "live_schema"
docker exec -e PGOPTIONS="$PGOPTS_READONLY" "$SRC_CTR" \
    pg_dump -U postgres -d "$SRC_DB" -n live --schema-only --no-owner \
    --no-privileges --lock-wait-timeout=30s > "$OUT_ABS/live_schema.sql"
if grep -n -E 'FOREIGN KEY|^COPY |^INSERT |setval' "$OUT_ABS/live_schema.sql"; then
    die "live_schema.sql contains data, sequence values or foreign keys"
fi
grep -q '^CREATE SCHEMA live;' "$OUT_ABS/live_schema.sql" || die "live_schema.sql lacks CREATE SCHEMA live"
LIVE_TABLES=$(grep -c '^CREATE TABLE live\.' "$OUT_ABS/live_schema.sql" || true)
[[ "$LIVE_TABLES" == 4 ]] || die "live_schema.sql has $LIVE_TABLES tables, expected 4"
step_done live_schema

# --- manifest -----------------------------------------------------------------
note "manifest"
FINGERPRINT_AFTER=$(psql_ro "$SRC_CTR" "$SRC_DB" -At -c \
    "SELECT count(*) || '|' || coalesce(sum(selection_count), 0) || '|' || coalesce(max(last_selected_at)::text, '')
       FROM live.sense_selection_stats")
cp "$OUT_ABS/build_counts.tsv" "$OUT_ABS/public_counts.tsv"
cp "$OUT_ABS/build_digests.tsv" "$OUT_ABS/digests.tsv"
{
    echo "# Publish-build manifest (PARTITION C4). THIS file, not C1's counts,"
    echo "# is the reference for check_restore: master must be quiet during the"
    echo "# build, and an unfenced local search can write public.root_llm_attempts"
    echo "# and public.sense_translations between C1 and any build."
    echo "built_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "source_time=$SOURCE_TIME"
    echo "source_container=$SRC_CTR"
    echo "source_db=$SRC_DB"
    echo "git_head=$GIT_HEAD"
    echo "git_dirty_files=$GIT_DIRTY"
    echo "alembic=$SRC_ALEMBIC"
    echo "fingerprint_before=$FINGERPRINT_BEFORE"
    echo "fingerprint_after=$FINGERPRINT_AFTER"
    echo "source_server_version_num=$SRC_VERSION_NUM"
    echo "pg_dump_version=$PG_DUMP_VERSION"
    echo "pgvector_master=$SRC_PGVECTOR"
    echo "pgvector_build=$BUILD_PGVECTOR"
    echo "build_image=$IMAGE"
    echo "build_image_id=$BUILD_IMAGE_ID"
    echo "build_maintenance_work_mem=$MWM"
    echo "build_parallel_workers=$WORKERS"
    echo "build_jobs=$JOBS"
    echo "master_dump_bytes=$MASTER_DUMP_BYTES"
    echo "master_dump_sha256=$MASTER_DUMP_SHA"
    echo "reference_dump=reference_public.dump"
    echo "reference_dump_bytes=$SLIM_BYTES"
    echo "reference_dump_sha256=$SLIM_SHA"
    echo "counts_file=public_counts.tsv"
    echo "digests_file=digests.tsv"
    echo "live_schema_file=live_schema.sql"
    echo "sense_embeddings_indexes_file=master_sense_embeddings_indexes.tsv"
    echo "timings_file=timings.tsv"
} > "$OUT_ABS/manifest.txt"
cat "$OUT_ABS/manifest.txt"
[[ "$FINGERPRINT_BEFORE" == "$FINGERPRINT_AFTER" ]] \
    || note "WARNING: master's statistics fingerprint changed during the build (was it quiet?)"
step_done manifest
note "build complete: $OUT_ABS"
