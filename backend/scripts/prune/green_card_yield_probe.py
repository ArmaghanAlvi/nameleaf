"""
Breakdown-D Step 5 -- GREEN CARD YIELD probe (read-only).

THE QUESTIONS. Stage 7e specifies an ordering and "surname flood control"
but names no numbers, and nothing in Stages 1-6 measured what a real query
actually pulls out of the two join surfaces. Four things must be measured
before any cap is written into the service:

  1. YIELD        how many green cards does one query produce, uncapped, and
                  how is that split across mechanism / type / language?
  2. FLOOD        which single tokens carry the most names? A cap is only
                  worth having if a few tokens dominate.
  3. HEAD         how often does a tier-0 SURNAME sit at position 1? Stage 7e
                  orders tier before type, so this is the case where the
                  roadmap's own rule buries the main event.
  4. COST         what does the forced English pass add in wall clock, and
                  what do the two joins themselves cost?

WHAT THE ANSWERS DECIDE. DEFAULT_LIMIT / DEFAULT_PER_TOKEN_CAP /
DEFAULT_SURNAME_CAP in app/services/green_card_retrieval.py, which ship
PROVISIONAL and are set for real in Step 6 from this output.

The probe runs the service UNCAPPED. Capping here would measure the cap.

READ-ONLY, and it calls parallel_expand directly rather than the route -- the
route is what writes SenseSelectionStat, so re-running this never moves the
diff_reference baseline.

USAGE (from backend/):
  python3 scripts/prune/green_card_yield_probe.py --scope all
  python3 scripts/prune/green_card_yield_probe.py --scope nonlatin --examples 15
  python3 scripts/prune/green_card_yield_probe.py --token pure
"""
from __future__ import annotations

import argparse
import contextlib
import os
import statistics
import sys
import time
from collections import Counter

sys.path.insert(0, os.getcwd())

from sqlalchemy import select, text                                  # noqa: E402

from app.db.session import SessionLocal                              # noqa: E402
from app.models.generated_name import Language                       # noqa: E402
from app.models.semantic import (                                    # noqa: E402
    EstablishedName, EstablishedNameToken,
)
from app.services.green_card_retrieval import (                      # noqa: E402
    MECH_TOKEN,
    english_token_keys,
    fold,
    language_code_map,
    match_by_homograph,
    match_by_meaning_token,
    retrieve_green_cards,
    visible_index,
)
from app.services.parallel_expansion import parallel_expand          # noqa: E402
from app.services.root_llm import fence_query_time_llm            # noqa: E402
fence_query_time_llm()
from scripts.eval.capture_engine_reference import most_used_sense_id  # noqa: E402

PROBE_WORDS = ["brave", "light", "storm", "river", "calm",
               "joy", "shadow", "fierce", "gold", "whisper"]

# Same two scope shapes the regression harness uses, plus "all". `all` is the
# production default (every language enabled); `nonlatin` is where mechanism 2
# concentrates (findings 7.7); `ru_only` is the maximum relative cost of the
# forced pass -- one visible tree, one hidden one.
SCOPES: dict[str, list[str] | None] = {
    "all": None,
    "nonlatin": ["ja", "hi", "ar"],
    "ru_only": ["ru"],
}

UNCAPPED = 10 ** 9
WIDTH, DEPTH = 3, 2


def all_codes(db) -> list[str]:
    return [c for (c,) in db.execute(
        select(Language.code).where(Language.code.isnot(None))
        .order_by(Language.code)
    )]


def token_report(db, token: str, cap: int) -> None:
    """--token: what does ONE meaning token actually pull? The concrete
    answer to 'does a meaning word connect to names carrying it'."""
    rows = db.execute(
        select(EstablishedName.lemma, EstablishedName.name_type,
               EstablishedName.meaning_channel, EstablishedName.meaning_text,
               EstablishedName.cluster_id, Language.code)
        .join(EstablishedNameToken,
              EstablishedNameToken.established_name_id == EstablishedName.id)
        .join(Language, Language.id == EstablishedName.language_id)
        .where(EstablishedNameToken.token == token)
        .order_by(Language.code, EstablishedName.name_type,
                  EstablishedName.normalized_lemma)
    ).all()
    print("=" * 74)
    print(f"TOKEN {token!r}: {len(rows)} names carry it in their meaning")
    print("=" * 74)
    print(f"  by language: {dict(Counter(r[5] for r in rows).most_common())}")
    print(f"  by type    : {dict(Counter(r[1] for r in rows).most_common())}")
    print(f"  in a cluster: {sum(1 for r in rows if r[4] is not None)}")
    print()
    for lemma, ntype, chan, meaning, cluster, code in rows[:cap]:
        star = "*" if cluster is not None else " "
        print(f"  {star}{code}:{lemma!r} [{ntype}/{chan}] "
              f"{(meaning or '')[:58]!r}")
    print("\n  (* = has a variant cluster; that card ships a dropdown)\n")


def run_word(db, word: str, sid: int, codes: list[str], cap: int) -> dict:
    codes_by_id = language_code_map(db)

    # Warm-up round (untimed): populates module-level caches
    # (_language_order, _pivot_eligible_languages) and the DB buffer cache
    # identically for BOTH configurations, so neither timed call below is
    # biased by running second. Without this, whichever config happens to
    # run second benefits from caches the first one populated -- which is
    # exactly why the first --scope all run showed pass ON *faster* than
    # pass OFF, a result that is impossible on its face.
    with open(os.devnull, "w") as dn, contextlib.redirect_stdout(dn):
        parallel_expand(
            db, english_sense_id=sid, language_codes=list(codes),
            width=WIDTH, depth=DEPTH, min_length=0, max_length=30,
            include_english_pass=False,
        )
        parallel_expand(
            db, english_sense_id=sid, language_codes=list(codes),
            width=WIDTH, depth=DEPTH, min_length=0, max_length=30,
            include_english_pass=True,
        )

    t0 = time.perf_counter()
    with open(os.devnull, "w") as dn, contextlib.redirect_stdout(dn):
        px_off = parallel_expand(
            db, english_sense_id=sid, language_codes=list(codes),
            width=WIDTH, depth=DEPTH, min_length=0, max_length=30,
            include_english_pass=False,
        )
    t_off = time.perf_counter() - t0

    t0 = time.perf_counter()
    with open(os.devnull, "w") as dn, contextlib.redirect_stdout(dn):
        px_on = parallel_expand(
            db, english_sense_id=sid, language_codes=list(codes),
            width=WIDTH, depth=DEPTH, min_length=0, max_length=30,
            include_english_pass=True,
        )
    t_on = time.perf_counter() - t0

    english_nodes = list(px_on.english_pass)
    visible = px_on.interleaved

    # Raw (pre-collapse, pre-cap) match counts, straight off the two joins.
    language_ids = {lid for lid, c in codes_by_id.items() if c in set(codes)}
    vis = visible_index(visible, codes_by_id)
    tokens = english_token_keys(english_nodes, "en" in set(codes))
    t0 = time.perf_counter()
    m1 = match_by_meaning_token(db, tokens, language_ids, UNCAPPED)
    t_m1 = time.perf_counter() - t0
    t0 = time.perf_counter()
    m2 = match_by_homograph(db, vis, language_ids)
    t_m2 = time.perf_counter() - t0
    raw = fold(m1 + m2, vis, codes_by_id)

    t0 = time.perf_counter()
    cards = retrieve_green_cards(
        db, english_nodes=english_nodes, visible_nodes=visible,
        language_codes=list(codes), limit=UNCAPPED,
        per_token_cap=UNCAPPED, surname_cap=UNCAPPED,
    )
    t_retrieve = time.perf_counter() - t0

    by_mech: Counter = Counter()
    by_type: Counter = Counter()
    by_tier: Counter = Counter()
    by_lang: Counter = Counter()
    token_yield: Counter = Counter()
    gradient = hidden_trigger = 0
    for card in cards:
        for mech in card.mechanisms:
            by_mech[mech] += 1
        by_type[card.name.name_type] += 1
        by_tier[card.tier] += 1
        by_lang[card.language_code] += 1
        for tok in card.matched_tokens:
            token_yield[tok] += 1
        gradient += int(card.is_gradient)
        hidden_trigger += int(card.anchor_sense_id is None)

    head = cards[0] if cards else None
    head_is_tier0_surname = bool(
        head and head.tier == 0 and head.name.name_type == "surname")

    print("-" * 74)
    print(f"{word}  (english_pass={len(english_nodes)} nodes, "
          f"visible={len(visible)} nodes)")
    print(f"  raw matches m1={len(m1)} m2={len(m2)} "
          f"-> distinct names {len(raw)} -> after cluster collapse "
          f"{len(cards)}")
    print(f"  by mechanism {dict(by_mech)}")
    print(f"  by type      {dict(by_type.most_common())}")
    print(f"  by tier      {dict(sorted(by_tier.items()))}")
    print(f"  by language  {dict(by_lang.most_common(8))}")
    print(f"  gradient {gradient}  hidden-trigger {hidden_trigger}")
    print(f"  head: "
          + (f"{head.language_code}:{head.name.lemma} "
             f"[{head.name.name_type} tier{head.tier}]" if head else "(none)")
          + ("   <-- TIER-0 SURNAME HEADS THE LIST"
             if head_is_tier0_surname else ""))
    print(f"  timing ms: pass_off={t_off*1000:.0f} pass_on={t_on*1000:.0f} "
          f"delta={(t_on-t_off)*1000:.0f} "
          f"m1={t_m1*1000:.1f} m2={t_m2*1000:.1f} "
          f"retrieve={t_retrieve*1000:.1f}")
    print("  top tokens by yield: "
          + ", ".join(f"{t}={n}" for t, n in token_yield.most_common(8)))
    for card in cards[:cap]:
        mech = "+".join(sorted(
            "M1" if m == MECH_TOKEN else "M2" for m in card.mechanisms))
        print(f"      {card.language_code}:{card.name.lemma!r} "
              f"[{card.name.name_type} {card.name.gender} tier{card.tier} "
              f"{mech}{' GRADIENT' if card.is_gradient else ''}] "
              f"via {card.trigger.lemma!r}"
              f"{'' if card.trigger.visible else ' (hidden)'}  "
              f"{(card.name.meaning_text or '')[:40]!r}")

    return {
        "cards": len(cards), "raw": len(raw),
        "m1": len(m1), "m2": len(m2),
        "surname": by_type["surname"], "given": by_type["given"],
        "gradient": gradient, "hidden_trigger": hidden_trigger,
        "head_tier0_surname": head_is_tier0_surname,
        "t_off": t_off, "t_on": t_on,
        "t_m1": t_m1, "t_m2": t_m2, "t_retrieve": t_retrieve,
        "token_yield": token_yield,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--scope", default="all", choices=sorted(SCOPES))
    ap.add_argument("--examples", type=int, default=10)
    ap.add_argument("--token", default=None,
                    help="report which names carry ONE meaning token, then exit")
    ap.add_argument("--word", action="append", default=[])
    args = ap.parse_args()

    with SessionLocal() as db:
        db.execute(text("SET lock_timeout = '30s'"))
        if args.token:
            token_report(db, args.token, max(args.examples, 30))
            return

        codes = SCOPES[args.scope] or all_codes(db)
        words = args.word or PROBE_WORDS
        print(f"scope={args.scope}  languages={len(codes)}  "
              f"width={WIDTH} depth={DEPTH}")

        rows = []
        for word in words:
            sid = most_used_sense_id(db, word)
            if sid is None:
                print(f"{word}: no embedded visible sense, skipped")
                continue
            rows.append(run_word(db, word, sid, codes, args.examples))

        if not rows:
            return
        pooled: Counter = Counter()
        for r in rows:
            pooled.update(r["token_yield"])
        print("=" * 74)
        print(f"POOLED over {len(rows)} words, scope={args.scope}")
        print("=" * 74)
        for key in ("cards", "raw", "m1", "m2", "given", "surname",
                    "gradient", "hidden_trigger"):
            vals = [r[key] for r in rows]
            print(f"  {key:<16} total={sum(vals):>7}  "
                  f"median={statistics.median(vals):>7.1f}  "
                  f"max={max(vals):>7}")
        print(f"  tier-0 surname heads the list: "
              f"{sum(r['head_tier0_surname'] for r in rows)}/{len(rows)} words")
        collapse = 1 - (sum(r["cards"] for r in rows)
                        / max(sum(r["raw"] for r in rows), 1))
        print(f"  cluster collapse removed {collapse*100:.1f}% of distinct names")
        for key, label in (("t_off", "parallel_expand, pass OFF"),
                           ("t_on", "parallel_expand, pass ON"),
                           ("t_m1", "mechanism 1 query"),
                           ("t_m2", "mechanism 2 query"),
                           ("t_retrieve", "retrieve_green_cards, total")):
            vals = [r[key] * 1000 for r in rows]
            print(f"  {label:<28} median={statistics.median(vals):8.1f} ms  "
                  f"max={max(vals):8.1f} ms")
        deltas = [(r["t_on"] - r["t_off"]) / max(r["t_off"], 1e-9)
                  for r in rows]
        print(f"  forced-pass overhead: median "
              f"{statistics.median(deltas)*100:.1f}%  "
              f"max {max(deltas)*100:.1f}%")
        print("\n  top tokens by yield, pooled:")
        for tok, n in pooled.most_common(20):
            print(f"    {n:>6}  {tok!r}")


if __name__ == "__main__":
    main()