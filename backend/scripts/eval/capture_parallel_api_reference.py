"""
ROUTE-level byte-identity harness for the PARALLEL path (Stage 8 gate).

WHY THIS EXISTS -- the same class of gap Breakdown D found in Step 2, one
layer up:

  * `diff_reference.py` gates the route, but `capture_api_current.py` builds
    its request with NO `languageCodes`, so both sides route down the LEGACY
    single-tree branch. It cannot see a change to the parallel branch.
  * `capture_parallel_reference.py` gates the parallel path, but calls
    `parallel_expand` DIRECTLY. It cannot see a change to the route.

Stage 8b changes the route ON the parallel branch. Neither existing gate
covers that intersection, so before Stage 8b lands there is no gate at all
on the thing being changed.

WHAT IS GATED: the YELLOW row sequence, `expandedSenses`, and
`treeSummaries`. Yellow rows are identified by their `sense-` id prefix,
which survives the gradient merge (a gradient card is the SAME row with its
category flipped, so it keeps `sense-{id}`). Standalone green cards carry
`name-{id}` and are counted but not gated -- they are the feature, and they
are zero on the "before" side by construction.

READ-ONLY. `record_sense_selection` is patched to a no-op below: it is the
route's only write, it is telemetry rather than result contract, and leaving
it live would make this harness drift its OWN baseline -- selection counts
feed `most_used_sense_id`, which picks the root senses.

USAGE (from backend/):
  python3 scripts/eval/capture_parallel_api_reference.py --out /tmp/api_px_before.json
  python3 scripts/eval/capture_parallel_api_reference.py --out /tmp/api_px_after.json \
      --reuse-from /tmp/api_px_before.json
  python3 scripts/eval/capture_parallel_api_reference.py \
      --diff /tmp/api_px_before.json /tmp/api_px_after.json
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys

sys.path.insert(0, os.getcwd())

from sqlalchemy import select, text                                   # noqa: E402

import app.api.routes.explore_v2 as route                             # noqa: E402
from app.db.session import SessionLocal                               # noqa: E402
from app.models.generated_name import Language                        # noqa: E402
from app.schemas.explore_v2 import ExploreV2Request                   # noqa: E402
from scripts.eval.capture_engine_reference import most_used_sense_id  # noqa: E402

# The route's only write. Silenced so re-running never shifts the root
# senses this harness resolves.
route.record_sense_selection = lambda *args, **kwargs: None

PROBE_WORDS = ["brave", "light", "storm", "river", "calm"]
CELLS = [(3, 2), (1, 1)]

# `all` is the production shape (every language enabled). `nonlatin` is where
# mechanism 2 concentrates (findings 7.7). `ru_only` is the worst case for
# the forced English pass (D-2: ~101% median overhead).
SCOPES: list[tuple[str, list[str] | None]] = [
    ("all", None),
    ("nonlatin", ["ja", "hi", "ar"]),
    ("ru_only", ["ru"]),
]


def all_codes(db) -> list[str]:
    return [c for (c,) in db.execute(
        select(Language.code).where(Language.code.isnot(None))
        .order_by(Language.code)
    )]


def capture_cell(db, sid: int, width: int, depth: int,
                 codes: list[str]) -> dict:
    req = ExploreV2Request(
        selectedSenseIds=[sid], queryText="", expansionCount=width,
        width=width, depth=depth, language=None,
        languageCodes=codes, minLength=0, maxLength=30,
    )
    with open(os.devnull, "w") as dn, contextlib.redirect_stdout(dn):
        resp = route.explore_v2(req, db=db)
    yellow = [r for r in resp.results if r.id.startswith("sense-")]
    green = [r for r in resp.results if not r.id.startswith("sense-")]
    return {
        # THE GATE.
        "yellow": [f"{r.languageCode}:{r.name}" for r in yellow],
        "yellow_sense_ids": [r.matchedSenseId for r in yellow],
        "expanded": [e.senseId for e in resp.expandedSenses],
        "summaries": [
            [s.languageCode, s.rootWord, s.rootRung, s.nodeCount,
             s.pivotedCount]
            for s in resp.treeSummaries
        ],
        # Informational only -- zero before Stage 8b, non-zero after.
        "green_count": len(green),
        "gradient_count": sum(
            1 for r in yellow if r.category == "word-established"),
        "green_sample": [r.name for r in green[:8]],
    }


def capture(out_path: str, reuse_from: str | None) -> None:
    with SessionLocal() as db:
        db.execute(text("SET lock_timeout = '30s'"))
        # Same precaution as capture_parallel_reference: this transaction
        # spans 5 words x 3 scopes x 2 cells = 30 route calls, each of which
        # can block on an HF Hub round-trip inside embed_query. Findings 15.0
        # records this exact failure class killing a Breakdown D capture.
        db.execute(text("SET idle_in_transaction_session_timeout = '600s'"))

        if reuse_from:
            with open(reuse_from) as fh:
                roots = json.load(fh)["roots"]
            print(f"reusing {len(roots)} roots from {reuse_from}")
        else:
            roots = {}
            for word in PROBE_WORDS:
                sid = most_used_sense_id(db, word)
                if sid is not None:
                    roots[word] = sid
            print(f"resolved roots: {roots}")

        every = all_codes(db)
        out: dict[str, dict] = {}
        for word, sid in roots.items():
            out[word] = {}
            for scope_name, scope_codes in SCOPES:
                codes = scope_codes if scope_codes is not None else every
                out[word][scope_name] = {}
                for width, depth in CELLS:
                    key = f"w{width}_d{depth}"
                    cell = capture_cell(db, sid, width, depth, codes)
                    out[word][scope_name][key] = cell
                    print(f"  {word:8s} {scope_name:9s} {key}  "
                          f"yellow={len(cell['yellow'])}  "
                          f"green={cell['green_count']}  "
                          f"gradient={cell['gradient_count']}")

    with open(out_path, "w") as fh:
        json.dump({"roots": roots, "cells": CELLS,
                   "scopes": [n for n, _ in SCOPES], "capture": out},
                  fh, ensure_ascii=False, indent=1)
    print(f"wrote {out_path}")


def diff(before_path: str, after_path: str) -> None:
    with open(before_path) as fh:
        before = json.load(fh)
    with open(after_path) as fh:
        after = json.load(fh)

    if before["roots"] != after["roots"]:
        print("!! ROOT SENSES DIFFER -- rerun 'after' with --reuse-from. "
              "Diff is meaningless.")
        return

    cells = changed = 0
    green_before = green_after = 0
    for word, scopes_b in before["capture"].items():
        for scope, cells_b in scopes_b.items():
            cells_a = after["capture"].get(word, {}).get(scope, {})
            for key, cell_b in cells_b.items():
                cell_a = cells_a.get(key, {})
                cells += 1
                green_before += cell_b.get("green_count", 0)
                green_after += cell_a.get("green_count", 0)
                label = f"{word} [{scope}] {key}"
                moved = False
                for field in ("yellow", "yellow_sense_ids", "expanded",
                              "summaries"):
                    if cell_b.get(field) != cell_a.get(field):
                        moved = True
                        print(f"{label}: {field.upper()} differs")
                        if field == "yellow":
                            print(f"    before {cell_b.get(field, [])[:6]}")
                            print(f"    after  {cell_a.get(field, [])[:6]}")
                if moved:
                    changed += 1

    print(f"\n{changed}/{cells} cells changed (yellow path)")
    print(f"green cards: {green_before} -> {green_after}")


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