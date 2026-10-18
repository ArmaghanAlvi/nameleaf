#!/usr/bin/env bash
# check_restore.sh -- verify a restored database against the build manifest
# (PARTITION C4; first run in C5). READ-ONLY: every session runs with
# default_transaction_read_only=on, so it may be pointed at any database.
#
# Prints PASS / FAIL / INFO lines; exits 1 if anything FAILs.
#   PASS/FAIL  public row counts == manifest (exact, per table)
#              synonyms digests (senses, lexemes) == manifest
#              embeddings digest == manifest
#              alembic == manifest == REQUIRED_ALEMBIC
#              106,398 established names; schema live present
#              exactly 1 HNSW index on sense_embeddings; no invalid index
#              every slug in public.sources is in live_sources.txt (so it
#              has a manifest and a /sources entry)
#   INFO       live row counts; orphan statistics/event rows (no matching
#              sense -- tolerated by readers); pgvector version; C1's
#              rowcounts_before.tsv cross-check (public may legitimately have
#              changed since C1: the manifest is the reference); a slug in
#              live_sources.txt that the database doesn't have
#
# Usage: check_restore.sh --container <ctr> --db <db> --build-dir <dir>
#        [--c1-counts <path>] [--live-sources <path>]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "$SCRIPT_DIR/lib.sh"

CTR="" DB="" BUILD_DIR="" C1_COUNTS="$SCRIPT_DIR/c1_master/rowcounts_before.tsv"
LIVE_SOURCES="$SCRIPT_DIR/../../data/source_manifests/live_sources.txt"
while [[ $# -gt 0 ]]; do
    case "$1" in
        --container) CTR="${2:-}"; shift 2 ;;
        --db) DB="${2:-}"; shift 2 ;;
        --build-dir) BUILD_DIR="${2:-}"; shift 2 ;;
        --c1-counts) C1_COUNTS="${2:-}"; shift 2 ;;
        --live-sources) LIVE_SOURCES="${2:-}"; shift 2 ;;
        *) die "unknown argument: $1" ;;
    esac
done
require_arg --container "$CTR"
require_arg --db "$DB"
require_arg --build-dir "$BUILD_DIR"
valid_identifier --db "$DB"
[[ -f "$BUILD_DIR/manifest.txt" ]] || die "no manifest.txt in $BUILD_DIR"
[[ -f "$LIVE_SOURCES" ]] || die "no live sources list at $LIVE_SOURCES"

manifest() { manifest_value "$BUILD_DIR" "$1"; }
TAB=$'\t'
FAILS=0
pass() { echo "PASS $*"; }
fail() { echo "FAIL $*"; FAILS=$((FAILS + 1)); }
info() { echo "INFO $*"; }

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# Row counts -----------------------------------------------------------------
psql_ro "$CTR" "$DB" -At -F "$TAB" < "$SCRIPT_DIR/public_counts.sql" > "$WORK/counts.tsv"
if diff "$BUILD_DIR/$(manifest counts_file)" "$WORK/counts.tsv" > "$WORK/counts.diff"; then
    pass "public_counts: $(wc -l < "$WORK/counts.tsv" | tr -d ' ') tables match the manifest"
else
    fail "public_counts: differ from the manifest:"
    cat "$WORK/counts.diff"
fi

# Digests --------------------------------------------------------------------
psql_ro "$CTR" "$DB" -At -F "$TAB" < "$SCRIPT_DIR/synonyms_digest.sql" > "$WORK/digests.tsv"
psql_ro "$CTR" "$DB" -At -F "$TAB" < "$SCRIPT_DIR/embeddings_digest.sql" >> "$WORK/digests.tsv"
while IFS="$TAB" read -r label rows digest; do
    got="$(awk -F "$TAB" -v l="$label" '$1 == l {print $2 "\t" $3}' "$WORK/digests.tsv")"
    if [[ "$got" == "$rows$TAB$digest" ]]; then
        pass "digest $label: $rows rows, $digest"
    else
        fail "digest $label: manifest '$rows $digest', restored '${got//$TAB/ }'"
    fi
done < "$BUILD_DIR/$(manifest digests_file)"

# Facts ----------------------------------------------------------------------
psql_ro "$CTR" "$DB" -At -F "$TAB" < "$SCRIPT_DIR/check_restore.sql" > "$WORK/facts.tsv"
fact() { awk -F "$TAB" -v k="$1" '$1 == k {print $2}' "$WORK/facts.tsv"; }

ALEMBIC="$(fact alembic)"
if [[ "$ALEMBIC" == "$(manifest alembic)" && "$ALEMBIC" == "$REQUIRED_ALEMBIC" ]]; then
    pass "alembic $ALEMBIC"
else
    fail "alembic '$ALEMBIC' (manifest $(manifest alembic), required $REQUIRED_ALEMBIC)"
fi
if [[ "$(fact established_names)" == 106398 ]]; then
    pass "established_names 106398"
else
    fail "established_names $(fact established_names), expected 106398"
fi
if [[ "$(fact live_schema)" == 1 ]]; then
    pass "schema live present"
else
    fail "schema live missing"
fi
if [[ "$(fact hnsw_indexes)" == 1 ]]; then
    pass "hnsw_indexes 1"
else
    fail "hnsw_indexes $(fact hnsw_indexes), expected 1"
fi
if [[ "$(fact invalid_indexes)" == 0 ]]; then
    pass "invalid_indexes 0"
else
    fail "invalid_indexes $(fact invalid_indexes)"
fi

# Data sources: every database slug needs a manifest (live_sources.txt) ------
{ grep -v '^[[:space:]]*#' "$LIVE_SOURCES" || true; } | awk 'NF {print $1}' | LC_ALL=C sort -u > "$WORK/live_sources"
awk -F "$TAB" '$1 == "source" {print $2}' "$WORK/facts.tsv" | LC_ALL=C sort -u > "$WORK/db_sources"
UNLISTED="$(LC_ALL=C comm -23 "$WORK/db_sources" "$WORK/live_sources")"
ABSENT="$(LC_ALL=C comm -13 "$WORK/db_sources" "$WORK/live_sources")"
if [[ -z "$UNLISTED" ]]; then
    pass "sources: $(wc -l < "$WORK/db_sources" | tr -d ' ') in the database, all in live_sources.txt"
else
    while read -r slug; do
        fail "source $slug is in the database but not in live_sources.txt (no manifest, no /sources entry)"
    done <<< "$UNLISTED"
fi
if [[ -n "$ABSENT" ]]; then
    while read -r slug; do
        info "source $slug is in live_sources.txt but not in the database"
    done <<< "$ABSENT"
fi

{ grep '^live_rows:' "$WORK/facts.tsv" || true; } | while IFS="$TAB" read -r k v; do
    info "${k#live_rows:} rows $v"
done
info "orphan_stats $(fact orphan_stats) (statistics rows with no matching sense)"
info "orphan_events $(fact orphan_events) (event rows with no matching sense)"
info "pgvector $(fact pgvector) (build manifest: master $(manifest pgvector_master), build $(manifest pgvector_build))"

# C1 cross-check (INFO only) -------------------------------------------------
if [[ -f "$C1_COUNTS" ]]; then
    awk -F "$TAB" '$1 == "public" {print $2 "\t" $3}' "$C1_COUNTS" | LC_ALL=C sort > "$WORK/c1.tsv"
    LC_ALL=C sort "$WORK/counts.tsv" > "$WORK/counts.sorted"
    LC_ALL=C join -t "$TAB" "$WORK/c1.tsv" "$WORK/counts.sorted" \
        | awk -F "$TAB" '$2 != $3 {printf "INFO c1_crosscheck %s: C1 %s, now %s\n", $1, $2, $3; d++}
                         END {if (!d) print "INFO c1_crosscheck: public counts equal C1 for every shared table"}'
else
    info "c1_crosscheck skipped: $C1_COUNTS not found"
fi

if (( FAILS > 0 )); then
    echo "check_restore: $FAILS FAIL(s)"
    exit 1
fi
echo "check_restore: all PASS"
