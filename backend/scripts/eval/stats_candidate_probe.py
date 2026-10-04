"""
Which usage-statistics senses were CANDIDATES during one search? (publishing
C2, Part A characterisation). READ-ONLY.

stats_trace.py explains a diff when the BEFORE output contains a statistics
sense. That misses the indirect case: a statistics sense that was a candidate
-- it won a duplicate-word group, or took the popularity bonus -- but did not
itself survive into the output (e.g. it fell below the score floor, or was
removed downstream), while still changing which other words did. This probe
re-runs the search on the CURRENT code and records every duplicate-word group
and every reranked candidate set, then reports which statistics senses were
present in each. A statistics sense present in a candidate set or group is
exactly the precondition for the removed tie-break or bonus to have acted.

Calls the expansion directly (the route, and its statistics write, are not
involved). The query-time LLM is fenced.

USAGE (from backend/):
  python3 scripts/eval/stats_candidate_probe.py --sense 7459 --width 3 \
      --depth 2 --languages en STAT_IDS.json
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys

sys.path.insert(0, os.getcwd())

from app.services.root_llm import fence_query_time_llm  # noqa: E402

fence_query_time_llm()

import app.services.vector_sense_search as vss  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.services.parallel_expansion import parallel_expand  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("stat_ids")
    ap.add_argument("--sense", type=int, required=True)
    ap.add_argument("--width", type=int, required=True)
    ap.add_argument("--depth", type=int, required=True)
    ap.add_argument("--languages", nargs="+", default=None)
    args = ap.parse_args()
    with open(args.stat_ids) as fh:
        stat_ids = set(json.load(fh))

    groups: list[list[int]] = []
    reranked_sets: list[list[int]] = []
    real_best, real_rerank = vss._best_duplicate, vss.rerank_candidates

    def best(group):
        groups.append([c.sense.id for c in group])
        return real_best(group)

    def rerank(*, candidates):
        reranked_sets.append([c.sense.id for c in candidates])
        return real_rerank(candidates=candidates)

    vss._best_duplicate = best
    vss.rerank_candidates = rerank

    with SessionLocal() as db:
        with open(os.devnull, "w") as dn, contextlib.redirect_stdout(dn):
            parallel_expand(db, english_sense_id=args.sense,
                            language_codes=args.languages, width=args.width,
                            depth=args.depth, min_length=0, max_length=30,
                            include_english_pass=args.languages is not None)
        db.rollback()

    multi = [g for g in groups if len(g) > 1]
    print(f"duplicate-word groups: {len(groups)} "
          f"(with more than one sense: {len(multi)})")
    hit_groups = [g for g in groups if set(g) & stat_ids]
    print(f"groups containing a statistics sense: {len(hit_groups)}")
    for g in hit_groups:
        print(f"  group {g}: statistics senses {sorted(set(g) & stat_ids)}"
              f"{'  <- tie-break could act' if len(g) > 1 else ''}")
    print(f"reranked candidate sets: {len(reranked_sets)}")
    hit_sets = [s for s in reranked_sets if set(s) & stat_ids]
    print(f"candidate sets containing a statistics sense (bonus could act): "
          f"{len(hit_sets)}")
    for s in hit_sets:
        print(f"  set of {len(s)}: statistics senses {sorted(set(s) & stat_ids)}")


if __name__ == "__main__":
    main()
