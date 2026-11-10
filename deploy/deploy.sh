#!/usr/bin/env bash
# deploy.sh -- deploy the current commit to the server, or roll back
# (PARTITION E1; roadmap 6e; E-0.7 6c-6f). Run on the Mac by the user, never
# by Claude Code (CLAUDE.md Safety rule 2).
#
#   bootstrap ALIAS
#       First publish only. The checks below, then send the repo and start
#       the database container alone (release.sh bootstrap). The restore and
#       gate come next (runbook steps 5-6), then `deploy`.
#   deploy ALIAS --base-url URL [--site-url URL] [--insecure]
#       The checks, send the repo, activate on the server (release.sh
#       activate: recreate only services whose image or config changed,
#       create any that don't exist yet, wait for health), then run the smoke
#       suite from here (deploy/smoke.py). On success, prune old tags and
#       releases (current + previous two kept).
#   rollback ALIAS --base-url URL [--insecure]
#       Re-point to the previous deployed tag and recreate what differs
#       (release.sh rollback), then the smoke suite. The database rollback
#       stays the runbook's previous-database switch (step 9).
#   status ALIAS
#       Read-only: tags, releases, images, containers on the server.
#
# Checks (bootstrap and deploy), all before anything is sent:
#   * the branch is main and `git status --porcelain` is empty;
#   * HEAD equals origin's main (`git ls-remote`, read-only; no fetch);
#   * GitHub Actions: `gh run list --commit HEAD` shows at least one run,
#     and every run is `completed success` (E-0.7 6f);
#   * the alias answers over SSH (key loaded in ssh-agent);
#   * on the server: both images at tag HEAD's sha12 (deploy/images.sh
#     ship), and the frontend's SITE_URL label equals the server's SITE_URL.
#
# The repo travels as `git archive HEAD | ssh ALIAS tar -x` into
# /srv/nameleaf/releases/<sha12>, then /srv/nameleaf/repo is re-pointed
# (an atomic symlink swap). That gives the server backend/scripts/ for the
# publish gate (C-6.7) and needs no GitHub access on the server.
#
# Stopping the old backend can take up to 330 s: in-flight searches finish
# first (stop_grace_period). Then the backend loads the model and pre-warms
# (healthcheck start_period 180 s). /api is down meanwhile; deploy off-peak.
#
# On failure: exit 1 and print the rollback command. Nothing is rolled back
# automatically.
#
# Exit codes: 0 ok; 1 activation, health or smoke failed (see the printed
# rollback command); 2 usage, refusal or precondition; 3 deployed and
# smoke-tested, but prune failed (nothing to roll back).
#
# Usage (from the repo root, on the Mac; see deploy/README.md):
#   deploy/deploy.sh deploy nameleaf-prod --base-url https://<site>
#   deploy/deploy.sh rollback nameleaf-prod --base-url https://<site>
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "$SCRIPT_DIR/lib.sh"

usage() { die "usage: deploy.sh bootstrap ALIAS | deploy ALIAS --base-url URL [--site-url URL] [--insecure] | rollback ALIAS --base-url URL [--insecure] | status ALIAS"; }

ALIAS=""
BASE_URL=""
SITE_URL=""
INSECURE=0

parse() {  # ALIAS [options]
    ALIAS="${1:-}"
    require_arg ALIAS "$ALIAS"
    case "$ALIAS" in -*) die "the first argument after the command must be the SSH alias (got '$ALIAS')" ;; esac
    valid_alias "$ALIAS"
    shift
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --base-url) BASE_URL="${2:-}"; shift 2 ;;
            --site-url) SITE_URL="${2:-}"; shift 2 ;;
            --insecure) INSECURE=1; shift ;;
            *) die "unknown argument: $1" ;;
        esac
    done
}

need_base_url() {
    require_arg --base-url "$BASE_URL"
    check_base_url "$BASE_URL"
    BASE_URL="${BASE_URL%/}"
}

# checks -> prints the tag. Every refusal happens before anything is sent.
checks() {
    local branch sha origin runs bad
    branch=$(git rev-parse --abbrev-ref HEAD)
    [[ "$branch" == "main" ]] || die "not on main (on '$branch'): deploys run only from main"
    tree_is_clean || die "the working tree is dirty: commit (and push) first"
    sha=$(git rev-parse HEAD)
    origin=$(git ls-remote origin refs/heads/main | cut -f1)
    [[ -n "$origin" ]] || die "could not read origin's main (git ls-remote origin refs/heads/main)"
    [[ "$sha" == "$origin" ]] || die "HEAD ($sha) is not origin's main ($origin): push first, or pull"
    runs=$(gh run list --commit "$sha" --json status,conclusion --jq '.[] | .status + " " + .conclusion') \
        || die "gh run list failed (is gh installed and authenticated?)"
    [[ -n "$runs" ]] || die "no CI run found for $sha: wait for GitHub Actions to start"
    bad=$(printf '%s\n' "$runs" | grep -v '^completed success$' || true)
    [[ -z "$bad" ]] || die "CI for $sha is not green yet: $(printf '%s' "$bad" | tr '\n' ';')"
    head_tag
}

send_release() {  # tag
    local tag="$1" root
    root=$(repo_root)
    note "sending the repo at $tag to $ALIAS:$SRV/releases/$tag"
    git -C "$root" archive --format=tar "$tag" | remote "$ALIAS" \
        "set -eu; d='$SRV/releases/$tag'; if [ -d \"\$d\" ]; then cat > /dev/null; echo 'release $tag already on the server'; exit 0; fi; t=\"\$d.incoming\"; rm -rf \"\$t\"; mkdir -p \"\$t\"; tar -x -C \"\$t\"; mv \"\$t\" \"\$d\"; echo 'release $tag received'"
}

rollback_hint() {
    echo "  To roll back the code: deploy/deploy.sh rollback $ALIAS --base-url ${BASE_URL:-<url>}" >&2
    echo "  Server state:          deploy/deploy.sh status $ALIAS" >&2
}

failed() {  # step
    echo "DEPLOY FAILED: $1. Nothing was rolled back automatically." >&2
    rollback_hint
    exit 1
}

smoke() {
    local args=(--base-url "$BASE_URL" --alias "$ALIAS")
    [[ -z "$SITE_URL" ]] || args+=(--site-url "$SITE_URL")
    (( ! INSECURE )) || args+=(--insecure)
    note "smoke suite against $BASE_URL"
    python3 "$SCRIPT_DIR/smoke.py" "${args[@]}"
}

cmd_bootstrap() {
    parse "$@"
    local tag
    tag=$(checks)
    check_reachable "$ALIAS"
    send_release "$tag"
    remote "$ALIAS" "$SRV/releases/$tag/deploy/server/release.sh" bootstrap "$tag"
    echo "Next: restore and gate (runbook steps 5-6), then: deploy/deploy.sh deploy $ALIAS --base-url <url>"
}

cmd_deploy() {
    parse "$@"
    need_base_url
    local tag
    tag=$(checks)
    check_reachable "$ALIAS"
    remote "$ALIAS" test -x "$SRV/repo/deploy/server/release.sh" \
        || die "the server has no release yet: the first publish starts with deploy.sh bootstrap"
    send_release "$tag"
    remote "$ALIAS" "$SRV/releases/$tag/deploy/server/release.sh" preflight "$tag"
    remote "$ALIAS" "$SRV/releases/$tag/deploy/server/release.sh" activate "$tag" \
        || failed "activation of $tag on the server"
    smoke || failed "smoke suite after activating $tag"
    if ! remote "$ALIAS" "$SRV/repo/deploy/server/release.sh" prune; then
        echo "WARNING: $tag is deployed and passed the smoke suite, but pruning old images failed (see above)." >&2
        exit 3
    fi
    note "deployed $tag"
}

cmd_rollback() {
    parse "$@"
    need_base_url
    check_reachable "$ALIAS"
    remote "$ALIAS" "$SRV/repo/deploy/server/release.sh" rollback || {
        echo "ROLLBACK FAILED on the server. Inspect: deploy/deploy.sh status $ALIAS" >&2
        exit 1
    }
    smoke || { echo "ROLLBACK DONE but the smoke suite failed. Inspect: deploy/deploy.sh status $ALIAS" >&2; exit 1; }
    note "rolled back; the smoke suite passed"
}

cmd_status() {
    parse "$@"
    check_reachable "$ALIAS"
    remote "$ALIAS" "$SRV/repo/deploy/server/release.sh" status
}

case "${1:-}" in
    bootstrap) shift; cmd_bootstrap "$@" ;;
    deploy) shift; cmd_deploy "$@" ;;
    rollback) shift; cmd_rollback "$@" ;;
    status) shift; cmd_status "$@" ;;
    *) usage ;;
esac
