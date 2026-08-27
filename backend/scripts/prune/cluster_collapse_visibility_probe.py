"""
Stage 13e -- does collapse_clusters ever discard the member the user can
actually see? (read-only)

THE QUESTION. `sort_key` has no gradient term, so when two members of one
cluster both match, the survivor is chosen on (tier, type, lemma, id) with
no regard for whether its spelling is on screen. If the discarded member was
in `vis_index` and the survivor was not, the query loses a merge it could
have had, and ships a standalone card that looks like it should have been
one card.

Whether that ever HAPPENS is unmeasured. A sort-key change ahead of this
measurement is the named anti-pattern, so this probe exists to earn or
refuse the change.

DECLINE THRESHOLD, agreed before running: if `bites` is 0, or under 1% of
collapsed multi-member clusters pooled, the sort term is DECLINED and
recorded as declined -- not carried forward as a to-do.

READ-ONLY: calls parallel_expand directly, never the route, so
SenseSelectionStat is untouched.

USAGE (from backend/):
  python3 scripts/prune/cluster_collapse_visibility_probe.py
  python3 scripts/prune/cluster_collapse_visibility_probe.py --scope nonlatin
"""
from __future__ import annotations

import argparse
import contextlib
import os
import sys
from collections import Counter

sys.path.insert(0, os.getcwd())

from sqlalchemy import select, text                                   # noqa: E402

from app.db.session import SessionLocal                               # noqa: E402
from app.models.generated_name import Language                        # noqa: E402
from app.services.green_card_retrieval import (                       # noqa: E402
    english_token_keys,
    fold,
    language_code_map,
    match_by_homograph,
    match_by_meaning_token,
    sort_key,
    visible_index,
    DEFAULT_PER_TOKEN_CAP,
)
from app.services.parallel_expansion import parallel_expand           # noqa: E402
from scripts.eval.capture_engine_reference import most_used_sense_id  # noqa: E402

PROBE_WORDS = ["brave", "light", "storm", "river", "calm",
               "joy", "shadow", "fierce", "gold", "whisper"]
SCOPES: dict[str, list[str] | None] = {
    "all": None, "nonlatin": ["ja", "hi", "ar"], "ru_only": ["ru"],
}
WIDTH, DEPTH = 3, 2


def run(db, scope: str) -> None:
    codes = SCOPES[scope]
    if codes is None:
        codes = [c for (c,) in db.execute(
            select(Language.code).where(Language.code.isnot(None))
            .order_by(Language.code))]
    codes_by_id = language_code_map(db)
    language_ids = {lid for lid, c in codes_by_id.items() if c in codes}

    totals: Counter = Counter()
    for word in PROBE_WORDS:
        sid = most_used_sense_id(db, word)
        if sid is None:
            print(f"{word}: no embedded visible sense, skipped")
            continue
        with open(os.devnull, "w") as dn, contextlib.redirect_stdout(dn):
            px = parallel_expand(
                db, english_sense_id=sid, language_codes=list(codes),
                width=WIDTH, depth=DEPTH, min_length=0, max_length=30,
                include_english_pass=True)

        vis = visible_index(px.interleaved, codes_by_id)
        tokens = english_token_keys(list(px.english_pass), "en" in codes)
        matches = match_by_meaning_token(db, tokens, language_ids,
                                         DEFAULT_PER_TOKEN_CAP)
        matches += match_by_homograph(db, vis, language_ids)
        cards = fold(matches, vis, codes_by_id)

        by_cluster: dict[int, list] = {}
        for c in cards:
            if c.name.cluster_id is not None:
                by_cluster.setdefault(c.name.cluster_id, []).append(c)

        contested = bites = 0
        for group in by_cluster.values():
            if len(group) < 2:
                continue
            contested += 1
            ordered = sorted(group, key=sort_key)
            survivor, losers = ordered[0], ordered[1:]
            surv_visible = (survivor.name.language_id,
                            survivor.name.normalized_lemma) in vis
            lost_visible = [c for c in losers
                            if (c.name.language_id,
                                c.name.normalized_lemma) in vis]
            if lost_visible and not surv_visible:
                bites += 1
                print(f"  BITE {word}: kept "
                      f"{survivor.language_code}:{survivor.name.lemma!r} "
                      f"(not visible), dropped "
                      f"{lost_visible[0].language_code}:"
                      f"{lost_visible[0].name.lemma!r} (visible)")
        totals["contested"] += contested
        totals["bites"] += bites
        print(f"{word:10s} cards={len(cards)}  "
              f"contested clusters={contested}  bites={bites}")

    n = max(totals["contested"], 1)
    print("=" * 70)
    print(f"POOLED scope={scope}: contested={totals['contested']}  "
          f"bites={totals['bites']}  ({totals['bites'] / n * 100:.2f}%)")
    print("DECLINE THRESHOLD: bites == 0 or < 1% of contested -> "
          "do not add the sort term.")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--scope", default="all", choices=sorted(SCOPES))
    args = ap.parse_args()
    with SessionLocal() as db:
        db.execute(text("SET lock_timeout = '30s'"))
        db.execute(text("SET idle_in_transaction_session_timeout = '600s'"))
        run(db, args.scope)


if __name__ == "__main__":
    main()