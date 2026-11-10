#!/usr/bin/env bash
# compose.sh -- `nameleaf-compose`, the only way to run docker compose on the
# server (PARTITION E1). setup.sh links it as /usr/local/bin/nameleaf-compose.
#
# Runs, from the current release (the /srv/nameleaf/repo symlink, resolved):
#   docker compose -f docker-compose.prod.yml -f deploy/compose.server.yml \
#     --env-file /srv/nameleaf/env/.env.production \
#     --env-file /srv/nameleaf/state/image.env  "$@"
#
# * compose.server.yml removes `build:`: the server never builds (E-0.7 6a).
# * image.env holds IMAGE_TAG, written only by release.sh. It comes second,
#   and "later files can override variables from earlier files" (Docker docs,
#   variable interpolation), so a stray IMAGE_TAG in the env file is ignored.
# * `cd -P` resolves the symlink, so compose sees the release's real path and
#   bind mounts (./deploy/Caddyfile) name that release; release.sh prune
#   keeps any release a container still mounts.
# * Refuses `--build` and `down` with -v/--volumes (the volume holds every
#   published database; CLAUDE.md Production stack).
#
# Usage (on the server): nameleaf-compose ps | logs backend | up -d --no-deps backend
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "$SCRIPT_DIR/lib.sh"

down=0
for arg in "$@"; do
    case "$arg" in
        down) down=1 ;;
        --build) die "the server never builds images (E-0.7 6a): build and ship them from the Mac (deploy/images.sh)" ;;
    esac
done
if (( down )); then
    for arg in "$@"; do
        case "$arg" in
            --volumes|--volumes=*) ;;
            --*) continue ;;
            -*v*) ;;                       # -v, or a cluster such as -tv
            *) continue ;;
        esac
        die "refusing 'down' with volume removal: the volume holds every published database"
    done
fi

[[ -L "$REPO_LINK" ]] || die "$REPO_LINK is not a release symlink yet: run deploy.sh bootstrap from the Mac"
[[ -f "$ENV_FILE" ]] || die "the server env file is missing under $SRV/env (from .env.production.example)"
[[ -f "$IMAGE_ENV" ]] || die "$IMAGE_ENV is missing: images are activated by deploy.sh, never by hand"

cd -P "$REPO_LINK"
exec docker compose -f docker-compose.prod.yml -f deploy/compose.server.yml \
    --env-file "$ENV_FILE" --env-file "$IMAGE_ENV" "$@"
