#!/usr/bin/env bash
# vector_parity.sh -- does a backend image embed the same query vectors as the
# Mac's CPU baseline? (PARTITION E1; B-1.7's method.) REPORT ONLY: there is no
# pass/fail threshold; the user reads the numbers and decides (E2).
#
#   encode --image IMAGE --out FILE [--alias A]
#       Runs backend/scripts/eval/vector_parity_probe.py INSIDE the image
#       with `docker run --rm --network none`, CPU, 2 threads, on the 64
#       texts in backend/scripts/eval/publishB_baseline/parity/
#       identity_texts.json (read-only mounts; the gate scripts are not in
#       the image). Writes the JSON (metadata + vectors) to FILE. The model
#       is loaded at the snapshot the app pins (DEFAULT_EMBEDDING_MODEL_
#       REVISION in backend/app/services/embedding_provider.py), the only one
#       in the image.
#       Without --alias: locally, mounting this repo's scripts/eval.
#       With --alias: on the server, mounting the deployed release's
#       scripts/eval (/srv/nameleaf/repo/backend/scripts/eval); the JSON
#       streams back over SSH. IMAGE is then a server image, e.g.
#       nameleaf-backend:<sha12>.
#   compare --image IMAGE A.json B.json
#       Runs the probe's --compare inside IMAGE locally (--network none;
#       it needs numpy) and prints B-1.7's metrics: bitwise-identical vectors,
#       max and mean absolute difference, and whether the rank order against
#       vector 0 is preserved. The Mac CPU baseline is
#       backend/scripts/eval/publishB_baseline/parity/vectors_mac_cpu.json.
#
# Exit codes: 0 done; 2 usage or refusal (missing argument, existing --out,
# bad alias); other non-zero: the probe or docker failed.
#
# Usage (from the repo root, on the Mac):
#   deploy/vector_parity.sh encode --image nameleaf-backend:<sha12> --alias nameleaf-prod --out parity_server.json
#   deploy/vector_parity.sh compare --image nameleaf-backend:<sha12> \
#       backend/scripts/eval/publishB_baseline/parity/vectors_mac_cpu.json parity_server.json
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "$SCRIPT_DIR/lib.sh"

PROBE="vector_parity_probe.py"
PROVIDER="backend/app/services/embedding_provider.py"
TEXTS="publishB_baseline/parity/identity_texts.json"

usage() { die "usage: vector_parity.sh encode --image IMAGE --out FILE [--alias A] | compare --image IMAGE A.json B.json"; }

valid_image() {
    [[ "${1:-}" =~ ^[a-z0-9][a-z0-9._/-]*:[A-Za-z0-9._-]+$ ]] || die "--image must be name:tag (got '${1:-}')"
}

encode() {
    local image="" out="" alias=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --image) image="${2:-}"; shift 2 ;;
            --out) out="${2:-}"; shift 2 ;;
            --alias) alias="${2:-}"; shift 2 ;;
            *) die "unknown argument: $1" ;;
        esac
    done
    require_arg --image "$image"
    require_arg --out "$out"
    valid_image "$image"
    [[ ! -e "$out" ]] || die "refusing to overwrite existing $out"

    local revision
    revision=$(sed -n 's/^DEFAULT_EMBEDDING_MODEL_REVISION = "\([0-9a-f]\{40\}\)"$/\1/p' "$(repo_root)/$PROVIDER")
    [[ -n "$revision" ]] || die "no DEFAULT_EMBEDDING_MODEL_REVISION in $PROVIDER"
    local run=(docker run --rm --network none -e TORCH_NUM_THREADS=2)
    local cmd=(python "/probe/$PROBE" --texts "/probe/$TEXTS" --threads 2 --revision "$revision")
    if [[ -z "$alias" ]]; then
        local eval_dir
        eval_dir="$(repo_root)/backend/scripts/eval"
        [[ -f "$eval_dir/$TEXTS" ]] || die "missing $eval_dir/$TEXTS"
        note "encoding locally in $image (--network none)"
        "${run[@]}" -v "$eval_dir:/probe:ro" "$image" "${cmd[@]}" > "$out.tmp"
    else
        valid_alias "$alias"
        check_reachable "$alias"
        note "encoding on $alias in $image (--network none)"
        remote "$alias" "${run[@]}" -v "$SRV/repo/backend/scripts/eval:/probe:ro" "$image" "${cmd[@]}" > "$out.tmp"
    fi
    [[ "$(head -c 1 "$out.tmp")" == "{" ]] || { rm -f "$out.tmp"; die "the probe wrote no JSON"; }
    mv -f "$out.tmp" "$out"
    note "wrote $out"
}

compare() {
    local image=""
    [[ "${1:-}" == "--image" ]] || usage
    image="${2:-}"
    shift 2 || usage
    valid_image "$image"
    [[ $# -eq 2 ]] || usage
    local a="$1" b="$2" eval_dir
    [[ -f "$a" && -f "$b" ]] || die "both JSON files must exist ($a, $b)"
    eval_dir="$(repo_root)/backend/scripts/eval"
    echo "REPORT ONLY (no threshold): $a vs $b, compared inside $image"
    docker run --rm --network none \
        -v "$eval_dir:/probe:ro" \
        -v "$(cd "$(dirname "$a")" && pwd)/$(basename "$a"):/in/a.json:ro" \
        -v "$(cd "$(dirname "$b")" && pwd)/$(basename "$b"):/in/b.json:ro" \
        "$image" python "/probe/$PROBE" --compare /in/a.json /in/b.json
}

case "${1:-}" in
    encode) shift; encode "$@" ;;
    compare) shift; compare "$@" ;;
    *) usage ;;
esac
