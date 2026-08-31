"""
Stage 12a -- GREEN PAYLOAD SIZE probe (read-only).

THE QUESTION. VARIANT_CAP/COGNATE_CAP were set in Breakdown E Step 6 to
bound PAYLOAD SIZE, not layout -- the dropdown is already a scrolling
container. Stage 12 raises them above the census maxima, so the only thing
that can veto the change is response size, and nothing has ever measured it.

WHY THE ROUTE AND NOT build_views. build_views returns dataclasses; the
thing that crosses the wire is ExploreV2Response. Serializing the response
is the only measurement that answers the question actually being asked.

READ-ONLY. `record_sense_selection` is patched to a no-op, same as
capture_parallel_api_reference: it is the route's only write, and leaving it
live would drift the roots this probe resolves.

USAGE (from backend/):
  python3 scripts/prune/green_payload_size_probe.py \
      --words pure,god,beloved,bright --out /tmp/g_size_before.json
  python3 scripts/prune/green_payload_size_probe.py \
      --words pure,god,beloved,bright --out /tmp/g_size_after.json
  python3 scripts/prune/green_payload_size_probe.py \
      --diff /tmp/g_size_before.json /tmp/g_size_after.json
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
from app.services.root_llm import fence_query_time_llm            # noqa: E402
fence_query_time_llm()
from scripts.eval.capture_engine_reference import most_used_sense_id  # noqa: E402

route.record_sense_selection = lambda *args, **kwargs: None

DEFAULT_WORDS = ["pure", "bright", "god", "beloved"]
CELLS = [(3, 2), (1, 1)]

# The gate, agreed BEFORE the numbers exist (12d).
HARD_CEILING_BYTES = 512 * 1024
SOFT_GROWTH = 0.30


def all_codes(db) -> list[str]:
    return [c for (c,) in db.execute(
        select(Language.code).where(Language.code.isnot(None))
        .order_by(Language.code)
    )]


def measure(db, sid: int, width: int, depth: int, codes: list[str]) -> dict:
    req = ExploreV2Request(
        selectedSenseIds=[sid], queryText="", expansionCount=width,
        width=width, depth=depth, language=None, languageCodes=codes,
        minLength=0, maxLength=30,
    )
    with open(os.devnull, "w") as dn, contextlib.redirect_stdout(dn):
        resp = route.explore_v2(req, db=db)
    body = resp.model_dump_json().encode("utf-8")
    greens = [r.green for r in resp.results if r.green is not None]
    return {
        "total_bytes": len(body),
        "green_bytes": sum(len(g.model_dump_json().encode("utf-8"))
                           for g in greens),
        "results": len(resp.results),
        "cards": len(greens),
        "variant_objects": sum(len(g.variants) for g in greens),
        "cognate_objects": sum(len(g.cognates) for g in greens),
        "variant_shown_max": max((len(g.variants) for g in greens),
                                 default=0),
        "variant_true_max": max((g.variantTotal for g in greens), default=0),
        "cognate_shown_max": max((len(g.cognates) for g in greens),
                                 default=0),
        "cognate_true_max": max((g.cognateTotal for g in greens), default=0),
        "cards_truncated": sum(
            1 for g in greens
            if g.variantTotal > len(g.variants)
            or g.cognateTotal > len(g.cognates)),
    }


def capture(words: list[str], out_path: str) -> None:
    with SessionLocal() as db:
        db.execute(text("SET lock_timeout = '30s'"))
        db.execute(text("SET idle_in_transaction_session_timeout = '600s'"))
        codes = all_codes(db)
        out: dict[str, dict] = {}
        roots: dict[str, int] = {}
        for word in words:
            sid = most_used_sense_id(db, word)
            if sid is None:
                print(f"{word}: no embedded visible sense, SKIPPED")
                continue
            roots[word] = sid
            out[word] = {}
            for width, depth in CELLS:
                key = f"w{width}_d{depth}"
                cell = measure(db, sid, width, depth, codes)
                out[word][key] = cell
                print(f"  {word:10s} {key}  "
                      f"total={cell['total_bytes']:>8}B  "
                      f"green={cell['green_bytes']:>8}B  "
                      f"cards={cell['cards']:>3}  "
                      f"v={cell['variant_objects']:>4} "
                      f"(true max {cell['variant_true_max']})  "
                      f"c={cell['cognate_objects']:>4} "
                      f"(true max {cell['cognate_true_max']})  "
                      f"truncated={cell['cards_truncated']}")
    with open(out_path, "w") as fh:
        json.dump({"roots": roots, "capture": out}, fh, indent=1)
    print(f"wrote {out_path}")


def diff(before_path: str, after_path: str) -> None:
    with open(before_path) as fh:
        before = json.load(fh)
    with open(after_path) as fh:
        after = json.load(fh)
    if before["roots"] != after["roots"]:
        print("!! ROOT SENSES DIFFER -- diff is meaningless.")
        return

    worst_after = 0
    growths: list[float] = []
    for word, cells_b in before["capture"].items():
        for key, b in cells_b.items():
            a = after["capture"].get(word, {}).get(key, {})
            if not a:
                continue
            grow = (a["total_bytes"] - b["total_bytes"]) / max(
                b["total_bytes"], 1)
            growths.append(grow)
            worst_after = max(worst_after, a["total_bytes"])
            print(f"{word:10s} {key}  "
                  f"total {b['total_bytes']:>8} -> {a['total_bytes']:>8} "
                  f"({grow*100:+5.1f}%)   "
                  f"green {b['green_bytes']:>8} -> {a['green_bytes']:>8}   "
                  f"v {b['variant_objects']:>4} -> {a['variant_objects']:>4}   "
                  f"truncated {b['cards_truncated']} -> "
                  f"{a['cards_truncated']}")

    growths.sort()
    median = growths[len(growths) // 2] if growths else 0.0
    print()
    print(f"worst single response after: {worst_after} B "
          f"(ceiling {HARD_CEILING_BYTES})")
    print(f"median growth: {median*100:+.1f}% (limit {SOFT_GROWTH*100:.0f}%)")
    if worst_after > HARD_CEILING_BYTES:
        print("VERDICT: FAIL on the hard ceiling -- the answer is lazy "
              "loading, NOT a lower cap.")
    elif median > SOFT_GROWTH:
        print("VERDICT: PASS on ceiling, over soft limit -- acceptable, "
              "record as a finding.")
    else:
        print("VERDICT: PASS")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--words", default=",".join(DEFAULT_WORDS))
    ap.add_argument("--out")
    ap.add_argument("--diff", nargs=2, metavar=("BEFORE", "AFTER"))
    args = ap.parse_args()
    if args.diff:
        diff(*args.diff)
        return
    if not args.out:
        ap.error("--out is required unless --diff is given")
    capture([w.strip() for w in args.words.split(",") if w.strip()],
            args.out)


if __name__ == "__main__":
    main()