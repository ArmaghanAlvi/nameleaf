# shellcheck shell=bash
# shellcheck disable=SC2034  # constants used by the scripts that source this file
# Shared helpers for the server-side scripts (PARTITION E1): release.sh,
# compose.sh, backup_live.sh, restore_live_test.sh. Sourced, never run.
# setup.sh is self-contained (it runs before the repo is on the server).
#
# Server layout (E-0.7 6i; deploy/README.md):
#   /srv/nameleaf/releases/<sha12>   one `git archive` per deployed commit
#   /srv/nameleaf/repo               symlink to the current release
#   /srv/nameleaf/env/.env.production  hand-written secrets (mode 700 dir)
#   /srv/nameleaf/state/image.env    IMAGE_TAG=<sha12>, written by release.sh
#   /srv/nameleaf/state/deployed     deployed tags, oldest first; last = current
#   /srv/nameleaf/state/history.log  every activate / rollback / prune
#   /srv/nameleaf/publish.lock       flock: backups vs publish steps
#
# NAMELEAF_SRV exists for the tests (a temp dir); on the server it is unset.
# The env file is read with grep, never sourced, and its values are never
# printed.

SRV="${NAMELEAF_SRV:-/srv/nameleaf}"
RELEASES="$SRV/releases"
REPO_LINK="$SRV/repo"
STATE="$SRV/state"
ENV_FILE="$SRV/env/.env.production"
IMAGE_ENV="$STATE/image.env"
DEPLOYED="$STATE/deployed"
HISTORY="$STATE/history.log"
DEPLOY_LOCK="$STATE/deploy.lock"
PUBLISH_LOCK="$SRV/publish.lock"
# The frontend's SITE_URL label, or "" (see deploy/lib.sh FINGERPRINT_FORMAT).
SITE_URL_FORMAT='{{with index .Config "Labels"}}{{index . "org.nameleaf.site_url"}}{{end}}'

die() {
    echo "ERROR: $*" >&2
    exit 2
}

note() {
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*"
}

require_arg() {  # name value
    [[ -n "${2:-}" ]] || die "missing required argument $1"
}

valid_tag() {
    [[ "${1:-}" =~ ^[0-9a-f]{12}$ ]] || die "image tag must be a 12-character git short SHA (got '${1:-}')"
}

# env_value KEY -> the value of KEY in the server env file (last wins, quotes
# stripped), or "" if unset. Never logged by callers.
env_value() {
    [[ -r "$ENV_FILE" ]] || die "cannot read the server env file under $SRV/env (see deploy/README.md)"
    local line
    line=$(grep -E "^$1=" "$ENV_FILE" | tail -1) || true
    line="${line#*=}"
    line="${line%\"}"; line="${line#\"}"
    line="${line%\'}"; line="${line#\'}"
    printf '%s' "$line"
}

# db_from_url URL -> the database name at the end of a DATABASE_URL.
db_from_url() {
    local db="${1##*/}"
    db="${db%%\?*}"
    printf '%s' "$db"
}

# The compose wrapper of the CURRENT release (the repo symlink).
compose() {
    "$REPO_LINK/deploy/server/compose.sh" "$@"
}

current_tag() {
    [[ -f "$IMAGE_ENV" ]] || { echo ""; return 0; }
    sed -n 's/^IMAGE_TAG=//p' "$IMAGE_ENV" | tail -1
}

# take_lock FILE FD-NUMBER: non-blocking flock, or die.
take_lock() {
    local file="$1"
    mkdir -p "$(dirname "$file")"
    exec 9>"$file"
    flock -n 9 || die "another operation holds $file; try again when it finishes"
}
