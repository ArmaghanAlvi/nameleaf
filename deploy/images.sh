#!/usr/bin/env bash
# images.sh -- build the production images on the Mac and ship them to the
# server (PARTITION E1; roadmap 6a, 6d, 6e; decisions E-0.7 6a, 6b, 6e).
#
#   build --site-url URL [--rehearsal]
#       docker build --platform linux/arm64 for backend/Dockerfile.prod and
#       frontend/Dockerfile.prod, tagged nameleaf-backend:<sha12> and
#       nameleaf-frontend:<sha12> (sha12 = `git rev-parse --short=12 HEAD`).
#       The frontend gets SITE_URL as a build arg (it is baked into the
#       pages, D-3b) and the label org.nameleaf.site_url=<URL>.
#       Refuses a dirty tree (the tag must mean that commit) and an empty,
#       non-https or local SITE_URL (localhost, *.localhost, 127.*,
#       0.0.0.0, [::1]).
#       No provenance/SBOM attestations and no per-commit label, so an
#       unchanged service rebuilt from the build cache keeps its image ID and
#       deploy.sh leaves its container alone (6d).
#       --rehearsal: local test builds only. Allows https://localhost and a
#       dirty tree, and tags `rehearsal`; `ship` refuses that tag.
#
#   ship --alias A [--tag T] [--replace-tag]
#       For each image at tag T (default: HEAD's sha12):
#         * refuses a frontend whose site_url label is local (D-3b);
#         * skips it if the server already has the same image under that
#           tag; re-tags on the server if it has it under another nameleaf
#           tag (an unchanged service); refuses if the server's tag holds a
#           different image, unless --replace-tag (E3's SITE_URL rebuild);
#         * otherwise `docker save | gzip | ssh A 'gunzip | docker load'`
#           (~1.1 GB for the backend), then checks the server's copy.
#       "The same image" = same layer diff IDs and runtime config
#       (lib.sh image_fingerprint): image IDs differ across image stores.
#
# Exit codes: 0 ok; 2 usage, refusal or precondition; other non-zero: a
# failed docker/ssh command (set -e).
#
# Usage (from the repo root, on the Mac):
#   deploy/images.sh build --site-url https://<site>
#   deploy/images.sh ship --alias nameleaf-prod
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "$SCRIPT_DIR/lib.sh"

usage() { die "usage: images.sh build --site-url URL [--rehearsal] | ship --alias A [--tag T] [--replace-tag]"; }

build() {
    local site="" rehearsal=0
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --site-url) site="${2:-}"; shift 2 ;;
            --rehearsal) rehearsal=1; shift ;;
            *) die "unknown argument: $1" ;;
        esac
    done
    require_arg --site-url "$site"
    site="${site%/}"
    local root tag
    root=$(repo_root)
    if (( rehearsal )); then
        [[ "$site" =~ ^https://[^/[:space:]]+$ ]] || die "SITE_URL must be https://<host> (got '$site')"
        tag="rehearsal"
        note "REHEARSAL build: tag '$tag', SITE_URL $site. These images are never shipped."
    else
        refuse_site_url "$site"
        tree_is_clean || die "the working tree is dirty: commit first, so the tag means that commit"
        tag=$(head_tag)
        valid_tag "$tag"
    fi

    note "building nameleaf-backend:$tag (linux/arm64)"
    docker build --platform linux/arm64 --provenance=false --sbom=false \
        -f "$root/backend/Dockerfile.prod" -t "nameleaf-backend:$tag" "$root/backend"
    note "building nameleaf-frontend:$tag (linux/arm64, SITE_URL=$site)"
    docker build --platform linux/arm64 --provenance=false --sbom=false \
        --build-arg "SITE_URL=$site" --label "$SITE_URL_LABEL=$site" \
        -f "$root/frontend/Dockerfile.prod" -t "nameleaf-frontend:$tag" "$root/frontend"

    local name
    for name in $IMAGES; do
        note "$name:$tag id=$(docker image inspect --format '{{.Id}}' "$name:$tag")"
    done
    if (( ! rehearsal )); then
        echo "Next: deploy/images.sh ship --alias <alias> --tag $tag"
    fi
}

# Runs on the server (bash -s): print "<ref>\t<fingerprint>" for every local
# tag of repository $1.
# shellcheck disable=SC2016  # expanded by the remote shell, not here
SERVER_LIST_TAGS='
set -euo pipefail
repo="$1"; fmt="$2"
for ref in $(docker image ls --format "{{.Repository}}:{{.Tag}}" "$repo"); do
    case "$ref" in *"<none>"*) continue ;; esac
    printf "%s\t%s\n" "$ref" "$(docker image inspect --format "$fmt" "$ref")"
done
'

ship_one() {  # alias name tag replace
    local alias="$1" name="$2" tag="$3" replace="$4"
    local ref="$name:$tag" local_fp server_list server_fp="" other="" line
    local_fp=$(image_fingerprint "$ref")
    [[ -n "$local_fp" ]] || die "no local image $ref: run deploy/images.sh build first"

    server_list=$(printf '%s' "$SERVER_LIST_TAGS" | remote_script "$alias" "$name" "'$FINGERPRINT_FORMAT'")
    while IFS= read -r line; do
        [[ -n "$line" ]] || continue
        if [[ "${line%%$'\t'*}" == "$ref" ]]; then
            server_fp="${line#*$'\t'}"
        elif [[ -z "$other" && "${line#*$'\t'}" == "$local_fp" ]]; then
            other="${line%%$'\t'*}"
        fi
    done <<< "$server_list"

    if [[ -n "$server_fp" ]]; then
        if [[ "$server_fp" == "$local_fp" ]]; then
            note "$ref: already on the server, skipped"
            return 0
        fi
        (( replace )) || die "the server's $ref is a different image. Re-run with --replace-tag only if that is intended (e.g. a SITE_URL rebuild, E3)"
        note "$ref: replacing the server's different image under this tag (--replace-tag)"
    elif [[ -n "$other" ]]; then
        note "$ref: the server has the same image as $other; tagging, no transfer"
        remote "$alias" docker tag "$other" "$ref"
        return 0
    fi

    note "$ref: sending (docker save | gzip | ssh | docker load)"
    docker save "$ref" | gzip | ssh -o BatchMode=yes "$alias" 'gunzip | docker load'
    server_fp=$(remote "$alias" docker image inspect --format "'$FINGERPRINT_FORMAT'" "$ref")
    [[ "$server_fp" == "$local_fp" ]] || die "$ref on the server does not match the local image after loading"
    note "$ref: loaded and verified"
}

ship() {
    local alias="" tag="" replace=0
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --alias) alias="${2:-}"; shift 2 ;;
            --tag) tag="${2:-}"; shift 2 ;;
            --replace-tag) replace=1; shift ;;
            *) die "unknown argument: $1" ;;
        esac
    done
    require_arg --alias "$alias"
    valid_alias "$alias"
    [[ -n "$tag" ]] || tag=$(head_tag)
    valid_tag "$tag"   # also refuses `rehearsal`

    local label
    label=$(docker image inspect --format "$SITE_URL_FORMAT" "nameleaf-frontend:$tag" 2>/dev/null) \
        || die "no local image nameleaf-frontend:$tag: run deploy/images.sh build first"
    [[ "$label" != "<no value>" ]] || label=""
    refuse_site_url "$label"
    note "shipping tag $tag to $alias (frontend SITE_URL $label)"

    check_reachable "$alias"
    local name
    for name in $IMAGES; do
        ship_one "$alias" "$name" "$tag" "$replace"
    done
    echo "Next: deploy/deploy.sh deploy $alias --base-url <url>   (bootstrap on the first publish)"
}

case "${1:-}" in
    build) shift; build "$@" ;;
    ship) shift; ship "$@" ;;
    *) usage ;;
esac
