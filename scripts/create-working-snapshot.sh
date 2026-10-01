#!/usr/bin/env bash
# Working-tree snapshot for the project chat.
#
# create-project-archive.sh uses `git archive HEAD`, so it holds only
# committed files and requires a clean tree. This script zips the folder as
# it is right now: uncommitted changes, untracked files and the gitignored
# notes/ included. Use it when a Claude Code section's work is uncommitted
# by design.
#
# Usage, from anywhere inside the repo:
#   scripts/create-working-snapshot.sh [label]
# Writes <repo>_<label>.zip next to the repo folder. With no label, a
# timestamp is used.
set -euo pipefail

ROOT="$(git rev-parse --show-toplevel)"
PARENT="$(dirname "$ROOT")"
NAME="$(basename "$ROOT")"
LABEL="${1:-$(date +%Y-%m-%d_%H%M)}"
OUT="$PARENT/${NAME}_${LABEL}.zip"

if [[ -e "$OUT" ]]; then
  echo "Refusing to overwrite existing $OUT"
  exit 1
fi

# Git state, so the reader can tell committed work from uncommitted work.
INFO_DIR="$(mktemp -d)"
trap 'rm -rf "$INFO_DIR"' EXIT
INFO="$INFO_DIR/SNAPSHOT_INFO.txt"
{
  echo "snapshot: $LABEL"
  echo "created:  $(date '+%Y-%m-%d %H:%M:%S %Z')"
  echo "HEAD:     $(git -C "$ROOT" log -1 --format='%h %s')"
  echo "branch:   $(git -C "$ROOT" rev-parse --abbrev-ref HEAD)"
  echo
  echo "== git status --short =="
  git -C "$ROOT" status --short
  echo
  echo "== git diff --stat (unstaged) =="
  git -C "$ROOT" diff --stat
  echo
  echo "== git diff --cached --stat (staged) =="
  git -C "$ROOT" diff --cached --stat
} > "$INFO"

cd "$PARENT"
zip -rq "$OUT" "$NAME" \
  -x "$NAME/.git/*" \
     "*/.venv/*" "*/node_modules/*" "*/.next/*" \
     "*/__pycache__/*" "*/.pytest_cache/*" "*/.mypy_cache/*" "*/.ruff_cache/*" "*.pyc" \
     "$NAME/backend/data/raw_sources/*" "$NAME/backend/data/extracted/*" \
     "$NAME/.claude/settings.local.json" \
     "*/.env" "*/.env.local" "*/.env.*.local" "*/.env.production" \
     "*.dump" "*.DS_Store"
zip -qj "$OUT" "$INFO"

# Safety net: a real env file that slipped past the excludes fails the run.
LEAKS="$(unzip -Z1 "$OUT" | grep -E '(^|/)\.env(\.[^/]+)?$' | grep -vE '\.example$' || true)"
if [[ -n "$LEAKS" ]]; then
  rm -f "$OUT"
  echo "ABORTED: env files would have been included, so the zip was deleted:"
  echo "$LEAKS"
  exit 1
fi

BYTES="$(wc -c < "$OUT" | tr -d ' ')"
echo "Created: $OUT ($(du -h "$OUT" | cut -f1))"
echo "Files:   $(unzip -Z1 "$OUT" | grep -vc '/$')"
grep -E '^(HEAD|branch):' "$INFO"
echo "Uncommitted entries: $(git -C "$ROOT" status --short | wc -l | tr -d ' ')"
if (( BYTES > 30 * 1024 * 1024 )); then
  echo "WARNING: over 30 MB. Largest files:"
  unzip -l "$OUT" | sort -k1 -n -r | sed -n '2,6p'
fi