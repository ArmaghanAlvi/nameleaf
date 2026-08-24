"""
Multilingual byte-identity harness (Phase C).

WHY THIS EXISTS: `diff_reference.py` CANNOT gate Phase C.
`capture_api_current.py` builds its ExploreV2Request with no `languageCodes`,
so BOTH sides of that diff route down explore_v2's legacy single-tree branch:
one English tree, no root selection, no interleave. Every C4 item changes
code on the PARALLEL path, and nothing captured it.

`root_selection_diff.py` covers part of the gap (the ladder), but explicitly
excludes vector fallback and pivot rescue, and covers no traversal at all.

READ-ONLY, and unlike capture_api_current.py it does NOT write
SenseSelectionStat -- that write lives in the route, which this bypasses. So
re-running this never moves its own baseline.

Root sense ids are STORED and REUSABLE: most_used_sense_id() reads
SenseSelectionStat, which drifts with ordinary UI use, so an "after" capture
that re-resolves would show diffs caused by usage rather than by code.

USAGE (from backend/):
  python3 scripts/eval/capture_parallel_reference.py --out /tmp/px_before.json
  python3 scripts/eval/capture_parallel_reference.py --out /tmp/px_after.json \
      --reuse-from /tmp/px_before.json
  python3 scripts/eval/capture_parallel_reference.py \
      --diff /tmp/px_before.json /tmp/px_after.json
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys

sys.path.insert(0, os.getcwd())

from sqlalchemy import text                                           # noqa: E402

from app.db.session import SessionLocal                               # noqa: E402
from app.services.parallel_expansion import parallel_expand           # noqa: E402
from scripts.eval.capture_engine_reference import most_used_sense_id  # noqa: E402

PROBE_WORDS = ["brave", "light", "storm", "river", "calm"]

# Two cells, not the full 4x4 grid. Root selection does not depend on
# width/depth, so extra cells re-pay ~20 select_root calls per word for no
# extra coverage. (3,2) exercises multi-level traversal and the pivot top-up;
# (1,1) exercises the root band and the interleave with a single hop.
CELLS = [(3, 2), (1, 1)]

# Language scopes. `None` is the pre-existing all-languages capture and lands
# under "capture", byte-comparable against the FROZEN estnames_baseline copy.
#
# The two explicit non-English scopes are new and land under "scoped". They
# exist because this harness could not see Stage 7a at all: every call above
# passes language_codes=None, which always includes English, so the forced
# English pass -- the branch that only runs when "en" is ABSENT -- was never
# executed by the gate meant to protect it. ru_only is the maximum relative
# cost (one visible tree, one hidden one); nonlatin is where mechanism 2
# concentrates (findings 7.7).
SCOPES: list[tuple[str, list[str] | None]] = [
    ("ru_only", ["ru"]),
    ("nonlatin", ["ja", "hi", "ar"]),
]


def capture_cell(db, sid: int, width: int, depth: int,
                 codes: list[str] | None = None) -> dict:
    with open(os.devnull, "w") as dn, contextlib.redirect_stdout(dn):
        px = parallel_expand(
            db, english_sense_id=sid, language_codes=codes,
            width=width, depth=depth, min_length=0, max_length=30,
            # Only the scoped (non-English) cells exercise the new branch;
            # for language_codes=None the pass is a no-op alias of the
            # already-built English tree.
            include_english_pass=codes is not None,
        )
    return {
        "trees": {
            code: {
                "root_rung": (t.root.rung if t.root
                              else ("selected" if code == "en" else None)),
                "root_sense_id": t.root.sense.id if t.root else None,
                "pivoted": t.pivoted_count,
                "words": [n.sense.lexeme.lemma for n in t.nodes],
                "sense_ids": [n.sense.id for n in t.nodes],
            }
            for code, t in px.trees.items()
        },
        "interleaved": [
            f"{n.sense.lexeme.language.code}:{n.sense.lexeme.lemma}"
            for n in px.interleaved
        ],
        "english_pass": [n.sense.lexeme.lemma for n in px.english_pass],
    }


def capture(out_path: str, reuse_from: str | None) -> None:
    with SessionLocal() as db:
        db.execute(text("SET lock_timeout = '30s'"))
        # Idle-in-transaction guard: embed_query can block on a slow/
        # unauthenticated HF Hub round-trip mid-transaction, and this
        # script's transaction spans many such calls across 5 words x
        # (2 cells + 2 scopes x 2 cells) = 25 parallel_expand calls. The
        # server default has been observed to kill the session before that
        # finishes. Scoped to this session only.
        db.execute(text("SET idle_in_transaction_session_timeout = '300s'"))
        if reuse_from:
            with open(reuse_from) as fh:
                roots = json.load(fh)["roots"]
            print(f"reusing {len(roots)} resolved root senses from {reuse_from}")
        else:
            roots = {}
            for word in PROBE_WORDS:
                sid = most_used_sense_id(db, word)
                if sid is not None:
                    roots[word] = sid
            print(f"resolved roots: {roots}")

        out: dict[str, dict] = {}
        scoped: dict[str, dict] = {}
        for word, sid in roots.items():
            out[word] = {}
            for width, depth in CELLS:
                key = f"w{width}_d{depth}"
                out[word][key] = capture_cell(db, sid, width, depth)
                n = len(out[word][key]["interleaved"])
                print(f"  {word:8s} {key}  interleaved={n}")
            scoped[word] = {}
            for name, codes in SCOPES:
                scoped[word][name] = {}
                for width, depth in CELLS:
                    key = f"w{width}_d{depth}"
                    cell = capture_cell(db, sid, width, depth, codes)
                    scoped[word][name][key] = cell
                    print(f"  {word:8s} {name:9s} {key}  "
                          f"interleaved={len(cell['interleaved'])}  "
                          f"english_pass={len(cell.get('english_pass', []))}")

    with open(out_path, "w") as fh:
        json.dump({"roots": roots, "cells": CELLS,
                   "scopes": [n for n, _ in SCOPES],
                   "capture": out, "scoped": scoped},
                  fh, ensure_ascii=False, indent=1)
    print(f"wrote {out_path}")


def diff(before_path: str, after_path: str) -> None:
    with open(before_path) as fh:
        before = json.load(fh)
    with open(after_path) as fh:
        after = json.load(fh)

    if before["roots"] != after["roots"]:
        print("!! ROOT SENSES DIFFER -- rerun the 'after' capture with "
              "--reuse-from the 'before' file. Diff is meaningless.")
        return

    totals = [0, 0, 0, 0]   # trees, changed trees, cells, changed cells

    def compare(label: str, cell_b: dict, cell_a: dict) -> None:
        totals[2] += 1
        changed = False
        if cell_b.get("interleaved") != cell_a.get("interleaved"):
            changed = True
            print(f"{label}: INTERLEAVE differs "
                  f"({len(cell_b['interleaved'])} -> "
                  f"{len(cell_a.get('interleaved', []))})")
        for code, tree_b in cell_b["trees"].items():
            totals[0] += 1
            tree_a = cell_a.get("trees", {}).get(code)
            if tree_a != tree_b:
                totals[1] += 1
                changed = True
                print(f"{label} [{code}]:")
                print(f"    rung  {tree_b['root_rung']} -> "
                      f"{(tree_a or {}).get('root_rung')}")
                print(f"    words {tree_b['words'][:6]}")
                print(f"       -> {(tree_a or {}).get('words', [])[:6]}")
        # english_pass is compared ONLY when both sides carry it. The Step-2
        # baseline predates the field, and a key that exists on one side only
        # is a harness-version difference, not an engine regression.
        if "english_pass" in cell_b and "english_pass" in cell_a:
            if cell_b["english_pass"] != cell_a["english_pass"]:
                changed = True
                print(f"{label}: ENGLISH PASS differs")
        if changed:
            totals[3] += 1

    for word, cells_b in before["capture"].items():
        cells_a = after["capture"].get(word, {})
        for key, cell_b in cells_b.items():
            compare(f"{word} {key}", cell_b, cells_a.get(key, {}))

    for word, scopes_b in before.get("scoped", {}).items():
        scopes_a = after.get("scoped", {}).get(word, {})
        for name, cells_b in scopes_b.items():
            cells_a = scopes_a.get(name, {})
            for key, cell_b in cells_b.items():
                compare(f"{word} [{name}] {key}", cell_b,
                        cells_a.get(key, {}))

    print(f"\n{totals[1]}/{totals[0]} trees changed")
    print(f"{totals[3]}/{totals[2]} cells changed")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out")
    ap.add_argument("--reuse-from")
    ap.add_argument("--diff", nargs=2, metavar=("BEFORE", "AFTER"))
    args = ap.parse_args()

    if args.diff:
        diff(args.diff[0], args.diff[1])
    elif args.out:
        capture(args.out, args.reuse_from)
    else:
        ap.error("pass --out or --diff")


if __name__ == "__main__":
    main()