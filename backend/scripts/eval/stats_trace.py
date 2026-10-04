"""
Trace each gate diff to usage statistics (publishing C2, Part A). READ-ONLY:
JSON in, text out; no database, no model.

Part A removes the only two places search results read usage statistics: the
duplicate-candidate tie-break (vector_sense_search) and the popularity bonus
(sense_reranker). Both act only on candidates that HAVE a statistics row: a
statistics sense was chosen over a same-word sibling, or boosted into the
results. So a Part A diff can only originate in a tree (or cell) whose
BEFORE sequence contains one of the statistics senses. Downstream nodes of a
changed node move too, but they sit in the same tree.

For every differing unit this reports whether its BEFORE sequence contains a
statistics sense ("explained") or not ("UNEXPLAINED" -- a finding).

Formats are auto-detected, as in device_diff_characterise.py:
  parallel_reference.json  unit = one language tree of one cell (sense_ids)
  parallel_api_reference   unit = one cell (yellow_sense_ids, expanded)
  engine_reference.json    unit = one grid cell (sense_id per node)
  roots.json               unit = one sense x language root cell

USAGE (from backend/):
  python3 scripts/eval/stats_trace.py BEFORE.json AFTER.json STAT_IDS.json
"""
from __future__ import annotations

import argparse
import json


def _report(name: str, units: list[tuple[str, list, list]],
            stat_ids: set[int]) -> None:
    differing = [(label, b, a) for label, b, a in units if b != a]
    explained = []
    unexplained = []
    for label, b, a in differing:
        hits = sorted({x for x in b if isinstance(x, int)} & stat_ids)
        (explained if hits else unexplained).append((label, hits))
    print(f"\n-- {name} --")
    print(f"units compared={len(units)}  differing={len(differing)}  "
          f"explained by a statistics sense={len(explained)}  "
          f"UNEXPLAINED={len(unexplained)}")
    for label, hits in explained:
        print(f"  explained   {label}: statistics senses in before = {hits}")
    for label, _ in unexplained:
        print(f"  UNEXPLAINED {label}")


def parallel(b: dict, a: dict, stat_ids: set[int]) -> None:
    units = []

    def cell(label: str, cb: dict, ca: dict) -> None:
        for code, tb in cb["trees"].items():
            ta = (ca.get("trees") or {}).get(code) or {}
            units.append((f"{label} [{code}]", tb["sense_ids"],
                          ta.get("sense_ids", [])))

    for word, cells in b["capture"].items():
        for key, cb in cells.items():
            cell(f"{word} {key}", cb, a["capture"].get(word, {}).get(key, {}))
    for word, scopes in b.get("scoped", {}).items():
        for scope, cells in scopes.items():
            for key, cb in cells.items():
                ca = a.get("scoped", {}).get(word, {}).get(scope, {}).get(key, {})
                cell(f"{word} [{scope}] {key}", cb, ca)
    _report("parallel_reference trees", units, stat_ids)


def api(b: dict, a: dict, stat_ids: set[int]) -> None:
    yellow, expanded = [], []
    for word, scopes in b["capture"].items():
        for scope, cells in scopes.items():
            for key, cb in cells.items():
                ca = a["capture"].get(word, {}).get(scope, {}).get(key, {})
                label = f"{word} [{scope}] {key}"
                yellow.append((label, cb["yellow_sense_ids"],
                               ca.get("yellow_sense_ids", [])))
                expanded.append((label, cb["expanded"], ca.get("expanded", [])))
    _report("parallel_api yellow rows", yellow, stat_ids)
    _report("parallel_api expandedSenses", expanded, stat_ids)


def engine(b: dict, a: dict, stat_ids: set[int]) -> None:
    units = []
    for word, data in b.items():
        if "skipped" in data:
            continue
        for key, cb in data["cells"].items():
            ca = a.get(word, {}).get("cells", {}).get(key, [])
            units.append((f"{word} {key}", [c["sense_id"] for c in cb],
                          [c["sense_id"] for c in ca]))
    _report("engine_reference cells", units, stat_ids)


def roots(b: dict, a: dict, stat_ids: set[int]) -> None:
    units = []
    for sid, cell_b in b["roots"].items():
        cell_a = a["roots"].get(sid, {})
        for code in b["languages"]:
            vb, va = cell_b.get(code), cell_a.get(code)
            # The before "sequence" is the English sense and its root; the
            # after side only needs to say whether the cell changed.
            before = [int(sid)] + ([vb[0]] if vb else [])
            units.append((f"{sid}/{code}", before,
                          before if vb == va else [None]))
    _report("root_selection cells", units, stat_ids)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("before")
    ap.add_argument("after")
    ap.add_argument("stat_ids")
    args = ap.parse_args()
    with open(args.before) as fh:
        b = json.load(fh)
    with open(args.after) as fh:
        a = json.load(fh)
    with open(args.stat_ids) as fh:
        stat_ids = set(json.load(fh))
    print(f"before={args.before}\nafter ={args.after}\n"
          f"statistics senses={len(stat_ids)}")
    if "sample" in b:
        roots(b, a, stat_ids)
    elif "scoped" in b:
        parallel(b, a, stat_ids)
    elif "capture" in b:
        api(b, a, stat_ids)
    else:
        engine(b, a, stat_ids)


if __name__ == "__main__":
    main()
