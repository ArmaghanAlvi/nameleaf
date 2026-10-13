#!/usr/bin/env bash
# One gate set (publishing C3): the four captures, each pinned to a prior
# set's root ids with --reuse-from, wrapped in device_report.py, with the
# read-only DB pre-flight before and after. Configuration comes from the
# environment, e.g. EMBEDDING_DEVICE=cpu TORCH_NUM_THREADS=2
# HF_HUB_OFFLINE=1 RANKING_STATS_WRITE=0 for production's configuration.
#
# Usage (from backend/): scripts/eval/run_gate_set.sh <out_dir> <reuse_from_dir>
set -u
OUT="$1"; REUSE="$2"
[[ -d "$REUSE" ]] || { echo "no reuse dir: $REUSE"; exit 2; }
[[ -e "$OUT" ]] && { echo "refusing to overwrite existing $OUT"; exit 2; }
mkdir -p "$OUT"
LOG="$OUT/run.log"
say() { echo "$*" | tee -a "$LOG"; }
step() {
  local name="$1"; shift
  say "== $name $(date)"
  python3 scripts/eval/device_report.py "$@" > "$OUT/$name.log" 2>&1
  local rc=$?
  say "rc=$rc"
  [[ $rc -eq 0 ]] || { say "ABORT: $name failed (see $OUT/$name.log)"; exit "$rc"; }
}
say "== config EMBEDDING_DEVICE=${EMBEDDING_DEVICE-} TORCH_NUM_THREADS=${TORCH_NUM_THREADS-} HF_HUB_OFFLINE=${HF_HUB_OFFLINE-} RANKING_STATS_WRITE=${RANKING_STATS_WRITE-}"
python3 scripts/eval/db_preflight.py --label "before $OUT" 2>&1 | tee -a "$LOG"
step parallel_reference scripts/eval/capture_parallel_reference.py \
  --out "$OUT/parallel_reference.json" --reuse-from "$REUSE/parallel_reference.json"
step roots scripts/eval/root_selection_diff.py \
  --out "$OUT/roots.json" --reuse-from "$REUSE/roots.json"
step engine_reference scripts/eval/capture_engine_reference.py \
  --out "$OUT/engine_reference.json" --reuse-from "$REUSE/engine_reference.json"
say "== harness_patch_check $(date)"
python3 scripts/eval/harness_patch_check.py 2>&1 | tee -a "$LOG"
[[ ${PIPESTATUS[0]} -eq 0 ]] || { say "ABORT: harness patch check failed"; exit 1; }
step parallel_api_reference scripts/eval/capture_parallel_api_reference.py \
  --out "$OUT/parallel_api_reference.json" --reuse-from "$REUSE/parallel_api_reference.json"
python3 scripts/eval/db_preflight.py --label "after $OUT" 2>&1 | tee -a "$LOG"
say "== DONE $(date)"
