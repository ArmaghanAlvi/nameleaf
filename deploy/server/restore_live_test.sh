#!/usr/bin/env bash
# restore_live_test.sh -- prove a live-schema backup restores (PARTITION E1;
# roadmap 6f "test a restore of the live dump once"; run in E3 on the server).
#
#   restore_live_test.sh --dump FILE --scratch-db restoretest_<name>
#
# Creates the SCRATCH database, restores the dump's schema `live` into it
# (pg_restore -n live --exit-on-error, inside the db container), and prints
# each `live` table's row count. It never touches the current database:
#   * the name must start with restoretest_ (so never name_generator*);
#   * it must not be the database named in DATABASE_URL;
#   * it must not exist yet.
# It never drops anything: it prints the DROP DATABASE command for the user,
# who runs it after reading the counts (CLAUDE.md Safety rule 1).
#
# Exit codes: 0 restored; 2 usage, refusal or precondition; other non-zero:
# a failed command (set -e).
#
# Usage (on the server, as nameleaf):
#   /srv/nameleaf/repo/deploy/server/restore_live_test.sh \
#       --dump /mnt/data/backups/live/live_<db>_<stamp>.dump --scratch-db restoretest_e3
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "$SCRIPT_DIR/lib.sh"

dump="" scratch=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --dump) dump="${2:-}"; shift 2 ;;
        --scratch-db) scratch="${2:-}"; shift 2 ;;
        *) die "unknown argument: $1 (usage: restore_live_test.sh --dump FILE --scratch-db restoretest_<name>)" ;;
    esac
done
require_arg --dump "$dump"
require_arg --scratch-db "$scratch"
[[ "$scratch" =~ ^restoretest_[a-z0-9_]{1,40}$ ]] \
    || die "--scratch-db must match restoretest_[a-z0-9_]+ (got '$scratch')"
[[ -s "$dump" ]] || die "no such dump file (or empty): $dump"
[[ -r "$ENV_FILE" ]] || die "cannot read the server env file under $SRV/env"

current=$(db_from_url "$(env_value DATABASE_URL)")
[[ "$scratch" != "$current" ]] || die "refusing: '$scratch' is the current database"
user=$(env_value POSTGRES_USER)
[[ -n "$user" ]] || die "POSTGRES_USER is not set in the server env file"

cid=$(compose ps -q db)
[[ -n "$cid" ]] || die "no running db container (nameleaf-compose ps -q db is empty)"

psql_in() {  # db sql
    docker exec -i -e PGOPTIONS='-c lock_timeout=30s' "$cid" \
        psql -X -v ON_ERROR_STOP=1 -U "$user" -d "$1" -tA -c "$2"
}

exists=$(psql_in postgres "SELECT 1 FROM pg_database WHERE datname = '$scratch'")
[[ -z "$exists" ]] || die "refusing: database $scratch already exists (drop it first, or pick another name)"

note "creating scratch database $scratch"
psql_in postgres "CREATE DATABASE $scratch"
note "restoring schema live from ${dump##*/}"
docker exec -i -e PGOPTIONS='-c lock_timeout=30s' "$cid" \
    pg_restore -U "$user" -d "$scratch" -n live --exit-on-error < "$dump"

note "row counts in $scratch.live:"
for table in $(psql_in "$scratch" "SELECT tablename FROM pg_tables WHERE schemaname = 'live' ORDER BY 1"); do
    printf '  live.%s\t%s\n' "$table" "$(psql_in "$scratch" "SELECT count(*) FROM live.\"$table\"")"
done
echo
echo "Restore test done. The scratch database stays until you drop it yourself:"
echo "  docker exec -i \$(nameleaf-compose ps -q db) psql -U <POSTGRES_USER> -d postgres -c 'DROP DATABASE $scratch'"
