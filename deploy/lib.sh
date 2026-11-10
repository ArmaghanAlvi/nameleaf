# shellcheck shell=bash
# shellcheck disable=SC2034  # constants used by the scripts that source this file
# Shared helpers for the Mac-side deploy scripts (PARTITION E1): images.sh,
# deploy.sh, vector_parity.sh. Sourced, never run. See README.md.
#
# SAFETY MODEL
#   * No target has a default. The server is named only by an SSH alias
#     (e.g. nameleaf-prod, defined in ~/.ssh/config outside the repo), and
#     valid_alias() refuses anything that looks like a hostname or an IP
#     (a dot, an @, a colon), so no address can reach a command line or a
#     log from this repo (CLAUDE.md Safety rule 2).
#   * Every SSH call runs with BatchMode=yes: a missing key in ssh-agent
#     fails at once instead of prompting halfway through a pipe.
#   * Must run under macOS's /bin/bash 3.2: no mapfile, no associative
#     arrays, no ${x,,}, and no "${arr[@]}" on a possibly empty array.

# Fixed server layout (E-0.7 6i). Paths, not targets. NAMELEAF_SRV exists
# only so the tests (whose stub ssh runs commands locally) use a temp dir.
SRV="${NAMELEAF_SRV:-/srv/nameleaf}"
IMAGES="nameleaf-backend nameleaf-frontend"
SITE_URL_LABEL="org.nameleaf.site_url"

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

# An SSH alias from ~/.ssh/config: letters, digits, '-' and '_' only.
valid_alias() {  # alias
    [[ "${1:-}" =~ ^[A-Za-z][A-Za-z0-9_-]*$ ]] \
        || die "the SSH alias must be a plain ~/.ssh/config alias such as nameleaf-prod (got '${1:-}'): no hostnames, IPs or user@"
}

valid_tag() {  # tag -- a 12-character git short SHA
    [[ "${1:-}" =~ ^[0-9a-f]{12}$ ]] || die "image tag must be a 12-character git short SHA (got '${1:-}')"
}

lower() { echo "$1" | tr '[:upper:]' '[:lower:]'; }

# url_host URL -> the host part of an http(s) URL, lower-cased.
url_host() {
    local rest="${1#*://}"
    rest="${rest%%/*}"
    rest="${rest##*@}"
    case "$rest" in
        \[*\]*) rest="${rest%%]*}]" ;;   # [::1]:443 -> [::1]
        *) rest="${rest%%:*}" ;;
    esac
    lower "$rest"
}

# is_local_host HOST -> 0 when HOST is a loopback or unspecified address.
is_local_host() {
    case "$1" in
        ""|localhost|*.localhost|127.*|0.0.0.0|"[::1]"|::1|"[::]") return 0 ;;
    esac
    return 1
}

# refuse_site_url URL: the server's public SITE_URL. Must be https and must
# not be local (D-3b: an image built for https://localhost must never ship).
refuse_site_url() {
    local url="${1:-}" host
    [[ -n "$url" ]] || die "SITE_URL is empty"
    [[ "$url" =~ ^https://[^/[:space:]]+/?$ ]] \
        || die "SITE_URL must be https://<host> with no path (got '$url')"
    host=$(url_host "$url")
    if is_local_host "$host"; then
        die "refusing SITE_URL '$url': a local address would be baked into the frontend (D-3b)"
    fi
}

# check_base_url URL: where smoke/load requests go (from the Mac).
check_base_url() {
    [[ "${1:-}" =~ ^https?://[^/[:space:]]+/?$ ]] \
        || die "--base-url must be http(s)://<host> with no path (got '${1:-}')"
}

# remote ALIAS COMMAND...: one command on the server. Arguments are joined
# by ssh into one remote shell line, so pass only validated values.
remote() {
    local alias="$1"
    shift
    ssh -o BatchMode=yes "$alias" "$@"
}

# remote_script ALIAS [ARGS...] < script: a bash script read from stdin.
remote_script() {
    local alias="$1"
    shift
    ssh -o BatchMode=yes "$alias" bash -s -- "$@"
}

check_reachable() {  # alias
    remote "$1" true \
        || die "cannot reach '$1' over SSH. Is the alias in ~/.ssh/config, and the key loaded (ssh-add)?"
}

repo_root() {
    git rev-parse --show-toplevel
}

head_tag() {
    git rev-parse --short=12 HEAD
}

tree_is_clean() {
    [[ -z "$(git status --porcelain)" ]]
}

# image_fingerprint REF -> layers + config of a local image ("" if absent).
# Image IDs aren't comparable across image stores (the Mac uses containerd's,
# Findings E-1), so "the same image" means the same layer diff IDs and the
# runtime config fields. Named fields, not {{json .Config}}: newer Engine API
# versions drop deprecated Config fields, so the whole struct can render
# differently on two engines holding the same image. `index` rather than
# .Config.Labels: Docker 29's CLI renders Config as a map, and a missing key
# (an image without labels) is a template error, not an empty value (E1).
FINGERPRINT_FORMAT='{{json .RootFS.Layers}} {{json (index .Config "Cmd")}} {{json (index .Config "Entrypoint")}} {{json (index .Config "Env")}} {{json (index .Config "Labels")}} {{json (index .Config "User")}} {{json (index .Config "WorkingDir")}}'
# The frontend's SITE_URL label, or "" (also for an image without labels).
SITE_URL_FORMAT='{{with index .Config "Labels"}}{{index . "org.nameleaf.site_url"}}{{end}}'
image_fingerprint() {
    docker image inspect --format "$FINGERPRINT_FORMAT" "$1" 2>/dev/null || true
}
