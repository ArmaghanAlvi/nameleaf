#!/usr/bin/env bash
# release.sh -- server side of deploy.sh (PARTITION E1; roadmap 6e; E-0.7
# 6c, 6d, 6e). Run over SSH by deploy.sh as the `nameleaf` user, from the
# release being activated (/srv/nameleaf/releases/<sha12>/deploy/server/).
# Layout and state files: lib.sh.
#
#   preflight TAG   read-only: release dir, env file, both images at TAG,
#                   frontend site_url label == the env file's SITE_URL (D-3b)
#   bootstrap TAG   first publish only: preflight, point repo + IMAGE_TAG at
#                   TAG, start db (nothing else). Refuses once a backend
#                   container exists. The restore (runbook 5-6) comes next.
#   activate TAG    preflight, point repo + IMAGE_TAG at TAG, then
#                   `up -d --no-deps --no-build --pull never` for:
#                     * every service with no container yet (all of backend,
#                       frontend and caddy on the first deploy after
#                       bootstrap), and
#                     * backend / frontend when the image ID at TAG differs
#                       from the running container's, or their compose config
#                       changed (`config --hash`, computed with the container's
#                       own tag so a new tag alone doesn't count).
#                   An existing db or caddy is never recreated: if their
#                   compose file or Caddyfile changed since the previous
#                   release, the exact command is printed instead.
#                   Then waits for health (backend start_period is 180 s).
#   rollback        re-point repo + IMAGE_TAG to the previous deployed tag
#                   (drops the current one from `deployed`), recreate what
#                   differs, wait for health. Database rollback stays the
#                   runbook's previous-database switch.
#   prune           keep the last three deployed tags (current + previous
#                   two): untag other nameleaf images (`docker image rm`,
#                   never -f) and remove other release dirs -- except any
#                   tag a container uses or release a container mounts.
#   status          read-only summary.
#
# Stopping the old backend can take up to 330 s (stop_grace_period): uvicorn
# drains in-flight searches first (E-0.7 6c).
#
# Exit codes: 0 ok; 1 a service did not become ready (activate, bootstrap,
# rollback) or an image could not be removed (prune); 2 usage, refusal or
# precondition; other non-zero: a failed command (set -e).
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "$SCRIPT_DIR/lib.sh"

HEALTH_TIMEOUT="${NAMELEAF_HEALTH_TIMEOUT:-300}"
POLL_SECONDS="${NAMELEAF_POLL_SECONDS:-5}"
KEEP_TAGS=3
SERVICES="db caddy backend frontend"
DRIFT_FILES="docker-compose.prod.yml deploy/compose.server.yml deploy/Caddyfile"

usage() { die "usage: release.sh preflight TAG | bootstrap TAG | activate TAG | rollback | prune | status"; }

history() { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) $*" >> "$HISTORY"; }

# --- checks -------------------------------------------------------------------

preflight() {  # tag
    local tag="$1" site label name
    valid_tag "$tag"
    [[ -f "$RELEASES/$tag/docker-compose.prod.yml" ]] \
        || die "release $RELEASES/$tag is missing: deploy.sh sends it before activating"
    [[ -f "$ENV_FILE" ]] || die "the server env file is missing under $SRV/env (copy .env.production.example, E2)"
    for name in nameleaf-backend nameleaf-frontend; do
        docker image inspect --format '{{.Id}}' "$name:$tag" > /dev/null 2>&1 \
            || die "image $name:$tag is not on this server: run deploy/images.sh ship from the Mac"
    done
    site=$(env_value SITE_URL)
    site="${site%/}"
    [[ -n "$site" ]] || die "SITE_URL is not set in the server env file (D-3b: every compose command needs it)"
    case "$site" in
        *://localhost*|*://127.*|*://0.0.0.0*|*://\[::1\]*) die "the server env file's SITE_URL is local" ;;
    esac
    label=$(docker image inspect --format "$SITE_URL_FORMAT" "nameleaf-frontend:$tag")
    [[ "$label" != "<no value>" ]] || label=""
    label="${label%/}"
    [[ "$label" == "$site" ]] \
        || die "nameleaf-frontend:$tag was built for SITE_URL '$label', but this server's SITE_URL is '$site'. Rebuild with deploy/images.sh build --site-url <server SITE_URL> (D-3b)"
    note "preflight ok: release, env file, images and SITE_URL for $tag"
}

# --- state ------------------------------------------------------------------------

# rename_over SRC DST: rename(2), which replaces DST atomically and never
# follows a symlink at DST (mv would move SRC *into* the directory it names).
rename_over() {
    perl -e 'rename($ARGV[0], $ARGV[1]) or die "rename $ARGV[0] -> $ARGV[1]: $!\n"' "$1" "$2"
}

point_at() {  # tag -- swap the repo symlink and IMAGE_TAG, each atomically
    local tag="$1"
    mkdir -p "$STATE"
    ln -sfn "releases/$tag" "$SRV/repo.tmp"
    rename_over "$SRV/repo.tmp" "$REPO_LINK"
    printf 'IMAGE_TAG=%s\n' "$tag" > "$IMAGE_ENV.tmp"
    rename_over "$IMAGE_ENV.tmp" "$IMAGE_ENV"
}

push_deployed() {  # tag
    local last=""
    [[ -f "$DEPLOYED" ]] && last=$(tail -1 "$DEPLOYED")
    [[ "$last" == "$1" ]] || echo "$1" >> "$DEPLOYED"
}

# --- service plan -------------------------------------------------------------------

container_of() {  # service -> container id, or "" (stopped containers count)
    compose ps -a -q "$1" 2>/dev/null | head -1
}

# plan TAG -> the services to (re)create, one "service reason" per line.
plan() {
    local tag="$1" svc cid new_id cur_id cur_ref want have
    for svc in $SERVICES; do
        cid=$(container_of "$svc")
        if [[ -z "$cid" ]]; then
            echo "$svc no container yet"
            continue
        fi
        case "$svc" in db|caddy) continue ;; esac
        new_id=$(docker image inspect --format '{{.Id}}' "nameleaf-$svc:$tag")
        cur_id=$(docker inspect --format '{{.Image}}' "$cid")
        if [[ "$new_id" != "$cur_id" ]]; then
            echo "$svc image changed"
            continue
        fi
        cur_ref=$(docker inspect --format '{{.Config.Image}}' "$cid")
        want=$(IMAGE_TAG="${cur_ref##*:}" compose config --hash "$svc" | awk '{print $2}')
        have=$(docker inspect --format '{{index .Config.Labels "com.docker.compose.config-hash"}}' "$cid")
        if [[ "$want" != "$have" ]]; then
            echo "$svc compose config changed"
        fi
    done
}

# drift_notice OLD_RELEASE_DIR NEW_RELEASE_DIR
drift_notice() {
    local old="$1" new="$2" f changed=""
    [[ -n "$old" && -d "$old" && "$old" != "$new" ]] || return 0
    for f in $DRIFT_FILES; do
        cmp -s "$old/$f" "$new/$f" || changed="$changed $f"
    done
    [[ -n "$changed" ]] || return 0
    echo "NOTICE: changed since the previous release:$changed"
    echo "  deploy never recreates an existing db or caddy. Review the change, then if needed:"
    echo "    nameleaf-compose up -d --no-deps --no-build --pull never caddy   # Caddyfile / caddy config"
    echo "    nameleaf-compose up -d --no-deps --no-build --pull never db      # db config (restarts Postgres)"
}

health_of() {  # container id -> healthy / starting / unhealthy / running / exited ...
    docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$1"
}

# wait_ready SERVICE: healthy (if it has a healthcheck) or running.
wait_ready() {
    local svc="$1" waited=0 cid status
    while :; do
        cid=$(container_of "$svc")
        status="missing"
        [[ -z "$cid" ]] || status=$(health_of "$cid")
        case "$status" in
            healthy) note "$svc healthy"; return 0 ;;
            running)
                if [[ -z "$(docker inspect --format '{{if .State.Health}}h{{end}}' "$cid")" ]]; then
                    note "$svc running (no healthcheck)"; return 0
                fi ;;
            unhealthy|exited|dead) echo "ERROR: $svc is $status" >&2; return 1 ;;
        esac
        if (( waited >= HEALTH_TIMEOUT )); then
            echo "ERROR: $svc not ready after ${HEALTH_TIMEOUT}s (status: $status)" >&2
            return 1
        fi
        sleep "$POLL_SECONDS"
        waited=$((waited + POLL_SECONDS))
        (( POLL_SECONDS > 0 )) || waited=$((waited + 1))
    done
}

# converge TAG: apply plan(TAG), then wait for what was started and for the
# backend whatever happened.
converge() {
    local tag="$1" lines svcs="" svc
    lines=$(plan "$tag")
    if [[ -z "$lines" ]]; then
        note "nothing to recreate: every running image and config already matches $tag"
    else
        # shellcheck disable=SC2001  # prefixes every line
        echo "$lines" | sed 's/^/  will (re)create: /'
        while read -r svc _; do svcs="$svcs $svc"; done <<< "$lines"
        case " $svcs " in
            *" backend "*)
                if [[ -n "$(container_of backend)" ]]; then
                    note "stopping the old backend first: it may take up to 330 s while in-flight searches finish"
                fi ;;
        esac
        # shellcheck disable=SC2086  # svcs is a list of plain service names
        compose up -d --no-deps --no-build --pull never $svcs
        for svc in $svcs; do
            wait_ready "$svc" || return 1
        done
    fi
    case " $svcs " in *" backend "*) ;; *) wait_ready backend || return 1 ;; esac
}

# --- commands -------------------------------------------------------------------------

cmd_bootstrap() {
    local tag="${1:-}"
    require_arg TAG "$tag"
    preflight "$tag"
    take_lock "$DEPLOY_LOCK"
    if [[ -L "$REPO_LINK" && -n "$(container_of backend)" ]]; then
        die "a backend container already exists: bootstrap is for the first publish only (use deploy)"
    fi
    point_at "$tag"
    push_deployed "$tag"
    history "bootstrap $tag"
    compose up -d --no-deps --no-build --pull never db
    wait_ready db || exit 1
    note "bootstrap done: db is up on $tag. Next: the restore (runbook steps 5-6), then deploy.sh deploy."
}

cmd_activate() {
    local tag="${1:-}" old=""
    require_arg TAG "$tag"
    preflight "$tag"
    take_lock "$DEPLOY_LOCK"
    [[ -s "$DEPLOYED" ]] || die "nothing deployed on this server yet: the first publish starts with deploy.sh bootstrap"
    if [[ -L "$REPO_LINK" ]]; then
        old=$(cd -P "$REPO_LINK" && pwd)
    fi
    point_at "$tag"
    push_deployed "$tag"
    history "activate $tag"
    drift_notice "$old" "$(cd -P "$REPO_LINK" && pwd)"
    converge "$tag" || exit 1
    note "activated $tag"
}

cmd_rollback() {
    take_lock "$DEPLOY_LOCK"
    [[ -f "$DEPLOYED" ]] || die "nothing deployed yet"
    local n cur target
    n=$(wc -l < "$DEPLOYED" | tr -d ' ')
    (( n >= 2 )) || die "no previous tag to roll back to (deployed: $(tr '\n' ' ' < "$DEPLOYED"))"
    cur=$(tail -1 "$DEPLOYED")
    target=$(tail -2 "$DEPLOYED" | head -1)
    note "rolling back $cur -> $target"
    preflight "$target"
    point_at "$target"
    sed '$d' "$DEPLOYED" > "$DEPLOYED.tmp"
    mv -f "$DEPLOYED.tmp" "$DEPLOYED"
    history "rollback $cur -> $target"
    converge "$target" || exit 1
    note "rolled back to $target. The database was not touched (runbook step 9 for that)."
}

# in_use -> "image:<name:tag>" and "release:<r>" lines for everything
# containers use.
in_use() {
    local cid ref src cur
    cur=$(cd -P "$REPO_LINK" 2>/dev/null && pwd) || cur=""
    [[ -z "$cur" ]] || echo "release:${cur##*/}"
    for cid in $(docker ps -a -q --filter label=com.docker.compose.project=nameleaf); do
        ref=$(docker inspect --format '{{.Config.Image}}' "$cid")
        case "$ref" in nameleaf-*:*) echo "image:$ref" ;; esac
        while IFS= read -r src; do
            case "$src" in
                "$RELEASES"/*)
                    src="${src#"$RELEASES"/}"; echo "release:${src%%/*}" ;;
                "$REPO_LINK"/*)
                    [[ -z "$cur" ]] || echo "release:${cur##*/}" ;;
            esac
        done < <(docker inspect --format '{{range .Mounts}}{{println .Source}}{{end}}' "$cid")
    done
}

cmd_prune() {
    take_lock "$DEPLOY_LOCK"
    [[ -f "$DEPLOYED" ]] || die "nothing deployed yet"
    local keep used name t dir base failed=0
    keep=" $(tail -"$KEEP_TAGS" "$DEPLOYED" | tr '\n' ' ')"
    used=" $(in_use | sort -u | tr '\n' ' ')"
    note "keeping tags:$keep"
    for name in nameleaf-backend nameleaf-frontend; do
        for t in $(docker image ls --format '{{.Tag}}' "$name"); do
            case "$keep" in *" $t "*) continue ;; esac
            case "$used" in *" image:$name:$t "*) note "kept $name:$t (a container uses it)"; continue ;; esac
            [[ "$t" != "<none>" ]] || continue
            if docker image rm "$name:$t" > /dev/null; then
                note "removed $name:$t"
            else
                echo "WARNING: could not remove $name:$t (left in place)" >&2
                failed=1
            fi
        done
    done
    for dir in "$RELEASES"/*; do
        [[ -d "$dir" ]] || continue
        base="${dir##*/}"
        case "$keep" in *" $base "*) continue ;; esac
        case "$used" in *" release:$base "*) note "kept release $base (a container mounts it)"; continue ;; esac
        rm -rf "$dir"
        note "removed release $base"
    done
    history "prune keep:$keep"
    return "$failed"
}

cmd_status() {
    echo "current tag: $(current_tag)"
    echo "repo -> $(readlink "$REPO_LINK" 2>/dev/null || echo '(not set)')"
    echo "deployed (oldest first):"
    if [[ -f "$DEPLOYED" ]]; then sed 's/^/  /' "$DEPLOYED"; fi
    echo "releases:"
    (ls -1 "$RELEASES" 2>/dev/null || true) | sed 's/^/  /'
    echo "images:"
    docker image ls --format '  {{.Repository}}:{{.Tag}} {{.ID}} {{.CreatedSince}}' --filter reference='nameleaf-*'
    echo "containers:"
    docker ps -a --filter label=com.docker.compose.project=nameleaf \
        --format '  {{.Names}} {{.Image}} {{.Status}}'
}

case "${1:-}" in
    preflight) shift; preflight "${1:-}" ;;
    bootstrap) shift; cmd_bootstrap "$@" ;;
    activate) shift; cmd_activate "$@" ;;
    rollback) shift; cmd_rollback ;;
    prune) shift; cmd_prune ;;
    status) shift; cmd_status ;;
    *) usage ;;
esac
