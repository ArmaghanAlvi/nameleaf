"""
A/B the UI's name-length filter against the engine.

diff_reference.py CANNOT measure this: capture_api_current.py hardcodes
maxLength=30 and sends no languageCodes, so both sides of that diff already
run at 30 on the English-only path. This probe sends what the UI actually
sends -- all language codes, the real breadth/depth grid -- at maxLength=20
and maxLength=30, and prints the cells that differ.

Run BEFORE removing the length control. Record the output.

Progress is printed after EVERY cell (flush=True), not just diffs, so a
long run stays observable. Each explore_v2 call is followed by an explicit
db.commit() so this script cannot itself leave an idle-in-transaction
session behind (see 2026-08 finding: a leaked "idle in transaction" session
from a prior interrupted run silently blocked this script for 50+ minutes).

Usage:
    python3 scripts/eval/length_filter_delta.py            # full run
    python3 scripts/eval/length_filter_delta.py --single    # one word, one cell
    python3 scripts/eval/length_filter_delta.py --words brave,light --grid b1_d1,b3d3
"""
import argparse
import contextlib
import json
import os
import sys
import time

sys.path.insert(0, os.getcwd())

from sqlalchemy import select
from app.db.session import SessionLocal
from app.models.generated_name import Language
from app.schemas.explore_v2 import ExploreV2Request
from app.api.routes.explore_v2 import explore_v2
from app.services.root_llm import fence_query_time_llm            # noqa: E402

fence_query_time_llm()

from scripts.eval.capture_engine_reference import most_used_sense_id

PROBE_WORDS = [
    "brave", "light", "storm", "river", "calm",
    "joy", "shadow", "fierce", "gold", "whisper",
]
GRID = [(b, d) for b in (0, 1, 2, 3) for d in (0, 1, 2, 3)]


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument(
        "--single", action="store_true",
        help="Run exactly one word x one grid cell (brave, b1_d1) and exit. "
             "Use this first to get a wall-clock time-per-cell before "
             "committing to the full run.",
    )
    p.add_argument(
        "--words", type=str, default=None,
        help="Comma-separated subset of PROBE_WORDS, e.g. brave,light",
    )
    p.add_argument(
        "--grid", type=str, default=None,
        help="Comma-separated subset of grid cells as bBdD, e.g. b1d1,b3d3",
    )
    return p.parse_args()


def all_codes(db):
    return [
        c for c in db.scalars(
            select(Language.code).where(Language.code.isnot(None))
        ).all()
    ]


def cell(db, sid, breadth, depth, codes, max_length):
    req = ExploreV2Request(
        selectedSenseIds=[sid],
        queryText="",
        expansionCount=breadth,
        width=breadth,
        depth=depth,
        language=None,
        languageCodes=codes,
        minLength=0,
        maxLength=max_length,
    )
    with open(os.devnull, "w") as dn, contextlib.redirect_stdout(dn):
        resp = explore_v2(req, db=db)
    # Explicit commit: explore_v2 may write SenseSelectionStat rows as a
    # side effect (same pattern as capture_api_current.py). Leaving that
    # open is exactly what caused the 52-minute hang -- commit immediately
    # so this script can never itself leak an idle-in-transaction session.
    db.commit()
    return [(r.name, r.languageCode) for r in resp.results]


def main():
    args = parse_args()

    words = PROBE_WORDS
    if args.words:
        words = [w.strip() for w in args.words.split(",")]

    grid = GRID
    if args.grid:
        grid = []
        for token in args.grid.split(","):
            token = token.strip().lower()
            b = int(token.split("d")[0][1:])
            d = int(token.split("d")[1])
            grid.append((b, d))

    if args.single:
        words = words[:1]
        grid = grid[:1]
        print(f"--single mode: word={words[0]!r}, grid_cell=b{grid[0][0]}d{grid[0][1]}", flush=True)

    total = diffs = 0
    report = {}
    t_start = time.monotonic()

    with SessionLocal() as db:
        codes = all_codes(db)
        print(f"languageCodes ({len(codes)}): {codes}", flush=True)
        print(f"words: {words}", flush=True)
        print(f"grid cells: {[f'b{b}d{d}' for b, d in grid]}", flush=True)
        print(f"total calls: {len(words) * len(grid) * 2}\n", flush=True)

        for word in words:
            sid = most_used_sense_id(db, word)
            if sid is None:
                print(f"{word}: skipped (no embedded visible sense)", flush=True)
                continue

            for breadth, depth in grid:
                total += 1
                t0 = time.monotonic()

                a = cell(db, sid, breadth, depth, codes, 20)
                t_mid = time.monotonic()

                b = cell(db, sid, breadth, depth, codes, 30)
                t_end = time.monotonic()

                key = f"{word} b{breadth}_d{depth}"
                differs = a != b

                print(
                    f"[{total}] {key}: "
                    f"20={t_mid - t0:.2f}s 30={t_end - t_mid:.2f}s "
                    f"{'DIFFERS' if differs else 'same'} "
                    f"(elapsed {t_end - t_start:.1f}s total)",
                    flush=True,
                )

                if differs:
                    diffs += 1
                    only_30 = [x for x in b if x not in a]
                    lost_20 = [x for x in a if x not in b]
                    report[key] = {"gained_at_30": only_30, "lost_from_20": lost_20}
                    print(f"    gained at 30: {only_30}", flush=True)
                    print(f"    lost from 20: {lost_20}", flush=True)

    print(f"\n{diffs}/{total} cells differ between maxLength=20 and maxLength=30", flush=True)
    print(f"total wall time: {time.monotonic() - t_start:.1f}s", flush=True)

    if not args.single:
        with open("scripts/eval/length_filter_delta.json", "w") as f:
            json.dump(report, f, indent=2)
        print("wrote scripts/eval/length_filter_delta.json", flush=True)


if __name__ == "__main__":
    main()