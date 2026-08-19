#!/usr/bin/env bash
# Green-card census battery (N1/N2/N3). Read-only, DB-side — no Kaikki files,
# no writes. Mirrors probe_battery.sh's conventions.
#
#   ./scripts/prune/name_probe_battery.sh --smoke      # la only, capped
#   ./scripts/prune/name_probe_battery.sh              # all languages
set -uo pipefail

OUT="${OUT:-scripts/prune/probe_out/_names}"
mkdir -p "$OUT"

if [ "${1:-}" = "--smoke" ]; then
  echo "=== SMOKE (la, 5000 senses) ==="
  python3 scripts/prune/name_inventory_probe.py --lang la --limit 5000 --examples 8
  python3 scripts/prune/name_meaning_probe.py   --lang la --limit 5000 --examples 8
  python3 scripts/prune/name_join_probe.py      --lang la --limit 5000 --examples 8
  exit 0
fi

( set -x
  python3 scripts/prune/name_inventory_probe.py --all --examples 8 \
      > "$OUT/n1_inventory.txt" 2>&1
  python3 scripts/prune/name_meaning_probe.py   --all --examples 8 \
      > "$OUT/n2_meaning.txt"   2>&1
  python3 scripts/prune/name_join_probe.py      --all --examples 8 \
      > "$OUT/n3_join.txt"      2>&1
) 2>>"$OUT/_run.log"

echo "--- headlines ---"
grep -E "^LANG|SHIPPING SET|^  senses " "$OUT/n1_inventory.txt" | head -60
grep -E "^LANG|ANY CHANNEL|WITHOUT the loose" "$OUT/n2_meaning.txt"
grep -E "^LANG|tokens that ARE|keys in 2\+" "$OUT/n3_join.txt"
echo "done -> $OUT"