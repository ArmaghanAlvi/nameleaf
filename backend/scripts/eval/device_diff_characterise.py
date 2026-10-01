"""
Characterise the differences between two captures of the SAME gate taken on
different devices (publishing B1, Part 5). READ-ONLY: JSON in, text out; no
database, no model.

Each gate's own --diff answers "did anything change". This answers "what kind
of change": how many ranks moved and by how much, whether a difference is a
pure reorder of the same items or a set change, and -- where the capture
records scores -- whether each swapped pair was a near tie.

A NEAR TIE here means the two swapped items' recorded scores are equal at the
capture's stored precision (4 dp for engine anchored_score and root
similarity). The parallel captures record no scores, so for them only the
reorder shape is reported; it cannot say near-tie or not.

Auto-detects the four formats under publishB_baseline/:
  roots.json (root_selection_diff), parallel_reference.json,
  parallel_api_reference.json, engine_reference.json.

USAGE (from backend/):
  python3 scripts/eval/device_diff_characterise.py BEFORE.json AFTER.json
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from itertools import combinations

TIE_EPS = 1e-4 + 1e-9   # one unit at 4 dp, plus float slack


def _keyed(seq: list) -> list:
    """Make items unique by occurrence so duplicates compare by position."""
    seen: Counter = Counter()
    out = []
    for item in seq:
        seen[item] += 1
        out.append((item, seen[item]))
    return out


class Stats:
    def __init__(self, name: str) -> None:
        self.name = name
        self.units = 0
        self.changed = 0
        self.pure_reorder = 0
        self.set_change = 0
        self.length_change = 0
        self.moved_items = 0
        self.displacements: Counter = Counter()
        self.entered = 0
        self.left = 0
        self.flipped_pairs = 0
        self.flipped_scored = 0
        self.flipped_near_tie = 0
        self.flip_gaps: list[float] = []
        self.examples: list[str] = []

    def seq(self, label: str, b: list, a: list,
            score_b: dict | None = None) -> None:
        self.units += 1
        if b == a:
            return
        self.changed += 1
        kb, ka = _keyed(b), _keyed(a)
        pos_b = {k: i for i, k in enumerate(kb)}
        pos_a = {k: i for i, k in enumerate(ka)}
        shared = [k for k in kb if k in pos_a]
        entered = [k for k in ka if k not in pos_b]
        left = [k for k in kb if k not in pos_a]
        if len(b) != len(a):
            self.length_change += 1
        if entered or left:
            self.set_change += 1
        else:
            self.pure_reorder += 1
        self.entered += len(entered)
        self.left += len(left)
        moved = [(k, pos_a[k] - pos_b[k]) for k in shared
                 if pos_a[k] != pos_b[k]]
        self.moved_items += len(moved)
        for _, d in moved:
            self.displacements[abs(d)] += 1

        # Pairs of shared items whose relative order flipped.
        flips = []
        for x, y in combinations(shared, 2):
            if (pos_b[x] - pos_b[y]) * (pos_a[x] - pos_a[y]) < 0:
                flips.append((x, y))
        self.flipped_pairs += len(flips)
        for x, y in flips:
            if score_b and x[0] in score_b and y[0] in score_b:
                gap = abs(score_b[x[0]] - score_b[y[0]])
                self.flipped_scored += 1
                self.flip_gaps.append(gap)
                if gap <= TIE_EPS:
                    self.flipped_near_tie += 1

        if len(self.examples) < 12:
            maxd = max((abs(d) for _, d in moved), default=0)
            self.examples.append(
                f"{label}: len {len(b)}->{len(a)} moved={len(moved)} "
                f"max|d|={maxd} entered={len(entered)} left={len(left)} "
                f"flips={len(flips)}")

    def report(self) -> None:
        print(f"\n-- {self.name} --")
        print(f"units compared={self.units}  differing={self.changed}")
        if not self.changed:
            return
        print(f"  pure reorder (same items)={self.pure_reorder}  "
              f"set changed={self.set_change}  length changed={self.length_change}")
        print(f"  items entered={self.entered}  left={self.left}  "
              f"shared items that moved rank={self.moved_items}")
        if self.displacements:
            dist = ", ".join(f"|d|={d}:{n}" for d, n in
                             sorted(self.displacements.items()))
            print(f"  rank displacement distribution: {dist}")
        print(f"  flipped pairs={self.flipped_pairs}  "
              f"with recorded scores={self.flipped_scored}  "
              f"near-tie (equal at 4dp)={self.flipped_near_tie}")
        if self.flip_gaps:
            g = sorted(self.flip_gaps)
            print(f"  score gap of flipped pairs: min={g[0]:.4f} "
                  f"median={g[len(g)//2]:.4f} max={g[-1]:.4f}")
        for ex in self.examples:
            print(f"    {ex}")


class Fields:
    """Counts of scalar-field changes (rungs, root senses, summaries)."""

    def __init__(self) -> None:
        self.c: Counter = Counter()
        self.n: Counter = Counter()
        self.examples: list[str] = []

    def eq(self, field: str, label: str, b, a) -> None:
        self.n[field] += 1
        if b != a:
            self.c[field] += 1
            if len(self.examples) < 12:
                self.examples.append(f"{label} {field}: {b} -> {a}")

    def report(self) -> None:
        print("\n-- scalar fields --")
        for f in sorted(self.n):
            print(f"  {f}: {self.c[f]}/{self.n[f]} differ")
        for ex in self.examples:
            print(f"    {ex}")


def engine(b: dict, a: dict) -> None:
    s = Stats("engine_reference cells (word sequence, keyed by sense_id)")
    f = Fields()
    for word, db in b.items():
        da = a.get(word, {})
        if "skipped" in db:
            continue
        f.eq("root_sense_id", word, db.get("root_sense_id"),
             da.get("root_sense_id"))
        for key, cb in db["cells"].items():
            ca = da.get("cells", {}).get(key, [])
            score = {c["sense_id"]: c["anchored_score"] for c in cb}
            s.seq(f"{word} {key}", [c["sense_id"] for c in cb],
                  [c["sense_id"] for c in ca], score)
            wa = {c["sense_id"]: c["anchored_score"] for c in ca}
            for sid, sc in score.items():
                if sid in wa:
                    f.eq("anchored_score (shared items, 4dp)",
                         f"{word} {key} {sid}", sc, wa[sid])
    s.report()
    f.report()


def roots(b: dict, a: dict) -> None:
    if b["sample"] != a["sample"]:
        print("!! samples differ; not comparable")
        return
    f = Fields()
    sim_d = []
    cells = changed = 0
    for sid, cb in b["roots"].items():
        ca = a["roots"][sid]
        for code in b["languages"]:
            cells += 1
            vb, va = cb.get(code), ca.get(code)
            if vb == va:
                continue
            changed += 1
            label = f"{sid}/{code}"
            if vb is None or va is None:
                f.eq("present", label, vb is not None, va is not None)
                continue
            f.eq("root sense", label, vb[0], va[0])
            f.eq("rung", label, vb[2], va[2])
            f.eq("similarity (4dp)", label, vb[3], va[3])
            sim_d.append(abs(vb[3] - va[3]))
    print(f"\n-- root_selection cells --\ncells compared={cells}  differing={changed}")
    if changed:
        f.report()
        if sim_d:
            print(f"  |similarity delta| max={max(sim_d):.4f}")


def parallel(b: dict, a: dict) -> None:
    if b["roots"] != a["roots"]:
        print("!! root senses differ; not comparable")
        return
    trees = Stats("parallel trees (keyed by sense_id)")
    inter = Stats("parallel interleaved sequences")
    eng = Stats("parallel english_pass sequences")
    f = Fields()

    def cell(label: str, cb: dict, ca: dict) -> None:
        inter.seq(label, cb.get("interleaved", []), ca.get("interleaved", []))
        if "english_pass" in cb and "english_pass" in ca:
            eng.seq(label, cb["english_pass"], ca["english_pass"])
        for code, tb in cb["trees"].items():
            ta = ca.get("trees", {}).get(code) or {}
            tl = f"{label} [{code}]"
            trees.seq(tl, tb["sense_ids"], ta.get("sense_ids", []))
            f.eq("root_rung", tl, tb["root_rung"], ta.get("root_rung"))
            f.eq("root_sense_id", tl, tb["root_sense_id"], ta.get("root_sense_id"))
            f.eq("pivoted", tl, tb["pivoted"], ta.get("pivoted"))

    for word, cells_b in b["capture"].items():
        for key, cb in cells_b.items():
            cell(f"{word} {key}", cb, a["capture"].get(word, {}).get(key, {}))
    for word, scopes_b in b.get("scoped", {}).items():
        for name, cells_b in scopes_b.items():
            for key, cb in cells_b.items():
                ca = a.get("scoped", {}).get(word, {}).get(name, {}).get(key, {})
                cell(f"{word} [{name}] {key}", cb, ca)
    trees.report()
    inter.report()
    eng.report()
    f.report()


def api(b: dict, a: dict) -> None:
    if b["roots"] != a["roots"]:
        print("!! root senses differ; not comparable")
        return
    yellow = Stats("api yellow rows (keyed by matchedSenseId)")
    expanded = Stats("api expandedSenses")
    f = Fields()
    for word, scopes_b in b["capture"].items():
        for scope, cells_b in scopes_b.items():
            for key, cb in cells_b.items():
                ca = a["capture"].get(word, {}).get(scope, {}).get(key, {})
                label = f"{word} [{scope}] {key}"
                yellow.seq(label, cb["yellow_sense_ids"],
                           ca.get("yellow_sense_ids", []))
                expanded.seq(label, cb["expanded"], ca.get("expanded", []))
                f.eq("yellow labels", label, cb["yellow"], ca.get("yellow"))
                f.eq("summaries", label, cb["summaries"], ca.get("summaries"))
    yellow.report()
    expanded.report()
    f.report()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("before")
    ap.add_argument("after")
    args = ap.parse_args()
    with open(args.before) as fh:
        b = json.load(fh)
    with open(args.after) as fh:
        a = json.load(fh)
    print(f"before={args.before}\nafter ={args.after}")
    if "sample" in b:
        roots(b, a)
    elif "scoped" in b:
        parallel(b, a)
    elif "capture" in b:
        api(b, a)
    else:
        engine(b, a)


if __name__ == "__main__":
    main()
