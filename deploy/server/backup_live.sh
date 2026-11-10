#!/usr/bin/env bash
# backup_live.sh -- nightly off-site backup of schema `live` (PARTITION E1;
# roadmap 6f; E-0.7 6j). Installed in cron by setup.sh, runs on the server as
# `nameleaf`. `live` can't be rebuilt from the Mac; `public` can (the
# reference dump on the external drive is its backup), so only `live` is
# dumped.
#
#   backup_live.sh --keep-local-days N --keep-remote-days N
#
# Steps:
#   1. flock -n /srv/nameleaf/publish.lock. If a publish step holds it, log
#      "skipped: publish lock held" and exit 5 (operators wrap publish steps
#      in the same lock: deploy/README.md, runbook step 5).
#   2. The remote: BACKUP_RCLONE_PATH from the server env file, and the rclone
#      remote `nameleaf-backup` (configured in E3). Missing either: exit 3.
#   3. The CURRENT database (the name at the end of DATABASE_URL) and the dump
#      user (POSTGRES_USER), read with grep; values are never logged.
#   4. The db container from `nameleaf-compose ps -q db`, then inside it:
#      pg_dump -n live -Fc --lock-wait-timeout=30s with PGOPTIONS
#      lock_timeout=30s, to /mnt/data/backups/live/live_<db>_<UTC>.dump
#      (written to .tmp, renamed when complete).
#   5. rclone copy to nameleaf-backup:<BACKUP_RCLONE_PATH>.
#   6. Retention: local dumps older than N days deleted; remote dumps older
#      than N days deleted with `rclone delete --min-age Nd --include`.
#
# Logs to /mnt/data/backups/backup.log (cron appends this script's output).
#
# Exit codes: 0 ok; 2 usage or precondition (bad argument, env file, no db
# container); 3 rclone remote not configured (expected until E3); 4 internal
# error (a failed dump, copy or delete); 5 skipped: publish lock held.
#
# Restore test (E3): deploy/server/restore_live_test.sh, into a scratch
# database only.
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "$SCRIPT_DIR/lib.sh"

trap 'echo "INTERNAL ERROR (exit 4) at line $LINENO: $BASH_COMMAND" >&2; exit 4' ERR

BACKUP_ROOT="${NAMELEAF_BACKUP_ROOT:-/mnt/data/backups}"
REMOTE="nameleaf-backup"

keep_local="" keep_remote=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --keep-local-days) keep_local="${2:-}"; shift 2 ;;
        --keep-remote-days) keep_remote="${2:-}"; shift 2 ;;
        *) die "unknown argument: $1 (usage: backup_live.sh --keep-local-days N --keep-remote-days N)" ;;
    esac
done
require_arg --keep-local-days "$keep_local"
require_arg --keep-remote-days "$keep_remote"
[[ "$keep_local" =~ ^[1-9][0-9]*$ ]] || die "--keep-local-days must be a positive integer"
[[ "$keep_remote" =~ ^[1-9][0-9]*$ ]] || die "--keep-remote-days must be a positive integer"

[[ -r "$ENV_FILE" ]] || die "cannot read the server env file under $SRV/env"
[[ -L "$REPO_LINK" ]] || die "$REPO_LINK is missing: nothing deployed yet"
mkdir -p "$BACKUP_ROOT/live"
note "backup_live start (keep local ${keep_local}d, remote ${keep_remote}d)"

# 1. Never overlap a publish step.
exec 8>"$PUBLISH_LOCK"
if ! flock -n 8; then
    note "skipped: publish lock held ($PUBLISH_LOCK)"
    exit 5
fi

# 2. The off-site remote (E3).
path=$(env_value BACKUP_RCLONE_PATH)
if [[ -z "$path" ]]; then
    note "rclone remote not configured: BACKUP_RCLONE_PATH is empty in the server env file (set up in E3)"
    exit 3
fi
if ! rclone listremotes 2>/dev/null | grep -qx "$REMOTE:"; then
    note "rclone remote not configured: no '$REMOTE:' in rclone listremotes (set up in E3)"
    exit 3
fi

# 3. What to dump, and as whom. Values are not logged.
db=$(db_from_url "$(env_value DATABASE_URL)")
[[ "$db" =~ ^[a-z_][a-z0-9_]{0,62}$ ]] || die "DATABASE_URL in the server env file has no valid database name"
user=$(env_value POSTGRES_USER)
[[ -n "$user" ]] || die "POSTGRES_USER is not set in the server env file"

# 4. Dump inside the db container.
cid=$(compose ps -q db)
[[ -n "$cid" ]] || die "no running db container (nameleaf-compose ps -q db is empty)"
stamp=$(date -u +%Y%m%dT%H%M%SZ)
out="$BACKUP_ROOT/live/live_${db}_${stamp}.dump"
docker exec -e PGOPTIONS='-c lock_timeout=30s' "$cid" \
    pg_dump -U "$user" -d "$db" -n live -Fc --lock-wait-timeout=30s > "$out.tmp"
[[ -s "$out.tmp" ]] || { rm -f "$out.tmp"; echo "pg_dump wrote nothing" >&2; exit 4; }
mv -f "$out.tmp" "$out"
note "dumped schema live of the current database to ${out##*/} ($(wc -c < "$out" | tr -d ' ') bytes)"

# 5. Off-site copy.
rclone copy "$out" "$REMOTE:$path"
note "copied to $REMOTE:<BACKUP_RCLONE_PATH>"

# 6. Retention.
find "$BACKUP_ROOT/live" -name 'live_*.dump' -type f -mtime +"$keep_local" -print -delete \
    | sed 's/^/  local removed: /'
rclone delete --min-age "${keep_remote}d" --include 'live_*.dump' "$REMOTE:$path"
note "backup_live done"
