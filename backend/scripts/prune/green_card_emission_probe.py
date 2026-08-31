"""
Breakdown-E Step 5 -- GREEN CARD EMISSION probe (read-only).

THE QUESTIONS. Stage 9c specifies two dropdown groupings but names no size,
and nothing in Stages 1-8 has measured either of them. Five things must be
measured before a cap is written into green_card_view.py:

  1. VARIANT FAN-OUT   how big is the same-language cluster of a card that a
                       real search actually surfaces? D-8 measured cluster
                       COLLAPSE (1.0% pooled) but never the SIZE of the
                       clusters that survived.
  2. COGNATE FAN-OUT   how many cross-language EQUIV_EN edges point AT a hub
                       name? `FANOUT_CAP` (name_variants.py) bounds targets
                       going OUT of one gloss; nothing bounds how many other
                       names point IN. `John` is the worst case and has never
                       been counted.
  3. LABEL QUALITY     what share of cluster members have a DIRECT edge to
                       the card? Members without one fall back to the generic
                       "Related form" label, which is honest but uninformative
                       -- if that share is most of them, the dropdown is worth
                       less than 9c assumes.
  4. MERGE RATE        how many retrieved green cards merge onto a yellow row
                       (gradient) vs. ship standalone? Drives how many EXTRA
                       rows a response actually grows by.
  5. COST              queries and wall clock added by build_views, and the
                       row growth per response.

WHAT THE ANSWERS DECIDE. VARIANT_CAP and COGNATE_CAP in
app/services/green_card_view.py, which ship PROVISIONAL and are set for real
in Step 6 from this output.

Sections 1-3 run GLOBALLY over established_names (no search needed, no
embedding, seconds not minutes). Sections 4-5 run the real retrieval +
view pipeline over the probe words, uncapped -- capping here would measure
the cap.

READ-ONLY, and it calls parallel_expand directly rather than the route, so
re-running never writes SenseSelectionStat and never moves any baseline.

USAGE (from backend/):
  python3 scripts/prune/green_card_emission_probe.py --fanout-only
  python3 scripts/prune/green_card_emission_probe.py --scope all
  python3 scripts/prune/green_card_emission_probe.py --scope nonlatin
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

from sqlalchemy import event, func, select, text                     # noqa: E402

from app.db.session import SessionLocal, engine                      # noqa: E402
from app.models.generated_name import Language                       # noqa: E402
from app.models.semantic import (                                    # noqa: E402
    EstablishedName, EstablishedNameCluster, EstablishedNameEdge,
)
from app.services.green_card_retrieval import retrieve_green_cards   # noqa: E402
from app.services.green_card_view import build_views                 # noqa: E402
from app.services.parallel_expansion import parallel_expand          # noqa: E402
from scripts.eval.capture_engine_reference import most_used_sense_id  # noqa: E402
from app.services.root_llm import fence_query_time_llm            # noqa: E402

fence_query_time_llm()


PROBE_WORDS = ["brave", "light", "storm", "river", "calm",
               "joy", "shadow", "fierce", "gold", "whisper"]

SCOPES: dict[str, list[str] | None] = {
    "all": None,
    "nonlatin": ["ja", "hi", "ar"],
    "ru_only": ["ru"],
}

UNCAPPED = 10 ** 9
WIDTH, DEPTH = 3, 2


def _pct(values: list[int], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, int(q * len(ordered)))
    return float(ordered[idx])


def section_1_2_3(db) -> None:
    """Global fan-out census. No search, no embedding."""
    print("=" * 74)
    print("1. VARIANT FAN-OUT -- same-language cluster sizes")
    print("=" * 74)
    sizes = [row.size for row in db.execute(
        select(EstablishedNameCluster.size)) if row.size]
    by_type = Counter(
        row.name_type for row in db.execute(
            select(EstablishedNameCluster.name_type))
    )
    if sizes:
        print(f"  clusters={len(sizes)}  by type {dict(by_type)}")
        print(f"  size: median={statistics.median(sizes):.1f}  "
              f"p90={_pct(sizes, 0.90):.0f}  p99={_pct(sizes, 0.99):.0f}  "
              f"max={max(sizes)}")
        buckets = Counter(min(s, 40) // 5 * 5 for s in sizes)
        for lo in sorted(buckets):
            print(f"    {lo:>3}-{lo+4:<3} {buckets[lo]:>6}")
    else:
        print("  no clusters")

    print()
    print("=" * 74)
    print("2. COGNATE FAN-OUT -- cross-language edges pointing AT one name")
    print("=" * 74)
    # IN-degree: how many other-language names claim equivalence with this
    # one. This is the number FANOUT_CAP does NOT bound.
    rows = db.execute(
        select(EstablishedNameEdge.target_name_id, func.count())
        .where(EstablishedNameEdge.is_cross_language.is_(True))
        .group_by(EstablishedNameEdge.target_name_id)
    ).all()
    indeg = [n for _, n in rows]
    if indeg:
        print(f"  names with >=1 incoming cross-language edge: {len(indeg)}")
        print(f"  in-degree: median={statistics.median(indeg):.1f}  "
              f"p90={_pct(indeg, 0.90):.0f}  p99={_pct(indeg, 0.99):.0f}  "
              f"max={max(indeg)}")
        top = sorted(rows, key=lambda r: -r[1])[:15]
        names = {
            n.id: (n.lemma, n.name_type, n.language_id)
            for n in db.scalars(select(EstablishedName).where(
                EstablishedName.id.in_([r[0] for r in top]))).all()
        }
        codes = {row.id: row.code for row in db.execute(
            select(Language.id, Language.code))}
        print("  worst hubs:")
        for name_id, count in top:
            lemma, ntype, lang_id = names.get(name_id, ("?", "?", None))
            print(f"    {count:>4}  {codes.get(lang_id, '?')}:{lemma} "
                  f"[{ntype}]")
    else:
        print("  no cross-language edges")

    print()
    print("=" * 74)
    print("3. LABEL QUALITY -- direct-edge coverage inside clusters")
    print("=" * 74)
    # For each clustered name: how many co-members share a DIRECT edge with
    # it? Members without one render as the generic "Related form".
    members: dict[int, list[int]] = {}
    for name_id, cluster_id in db.execute(
        select(EstablishedName.id, EstablishedName.cluster_id)
        .where(EstablishedName.cluster_id.isnot(None))
    ):
        members.setdefault(cluster_id, []).append(name_id)
    adjacency: dict[int, set[int]] = {}
    for src, tgt in db.execute(
        select(EstablishedNameEdge.source_name_id,
               EstablishedNameEdge.target_name_id)
        .where(EstablishedNameEdge.is_cross_language.is_(False))
    ):
        adjacency.setdefault(src, set()).add(tgt)
        adjacency.setdefault(tgt, set()).add(src)
    direct = total = 0
    for member_ids in members.values():
        member_set = set(member_ids)
        for name_id in member_ids:
            others = member_set - {name_id}
            total += len(others)
            direct += len(others & adjacency.get(name_id, set()))
    if total:
        print(f"  member pairs: {total}   direct edge: {direct} "
              f"({direct / total * 100:.1f}%)")
        print(f"  falls back to 'Related form': "
              f"{(total - direct) / total * 100:.1f}%")
    else:
        print("  no clustered names")


def section_4_5(db, scope: str, examples: int) -> None:
    codes = SCOPES[scope]
    if codes is None:
        codes = [c for (c,) in db.execute(
            select(Language.code).where(Language.code.isnot(None))
            .order_by(Language.code))]

    print()
    print("=" * 74)
    print(f"4/5. MERGE RATE AND COST -- scope={scope}, {len(codes)} languages")
    print("=" * 74)

    counted: list[str] = []

    @event.listens_for(engine, "before_cursor_execute")
    def _count(conn, cursor, statement, params, context, executemany):
        counted.append(statement)

    rows = []
    try:
        for word in PROBE_WORDS:
            sid = most_used_sense_id(db, word)
            if sid is None:
                print(f"{word}: no embedded visible sense, skipped")
                continue

            with open(os.devnull, "w") as dn, contextlib.redirect_stdout(dn):
                px = parallel_expand(
                    db, english_sense_id=sid, language_codes=list(codes),
                    width=WIDTH, depth=DEPTH, min_length=0, max_length=30,
                    include_english_pass=True,
                )
            cards = retrieve_green_cards(
                db, english_nodes=list(px.english_pass),
                visible_nodes=px.interleaved, language_codes=list(codes),
            )

            counted.clear()
            t0 = time.perf_counter()
            views = build_views(db, cards, variant_cap=UNCAPPED,
                                cognate_cap=UNCAPPED)
            elapsed = time.perf_counter() - t0
            queries = len(counted)

            gradient = sum(1 for v in views if v.card.is_gradient
                           and v.card.homograph_anchor_sense_id is not None)
            variant_counts = [v.variant_total for v in views]
            cognate_counts = [v.cognate_total for v in views]
            generic = sum(1 for v in views for x in v.variants
                          if not x.is_direct)
            variant_rows = sum(variant_counts)

            print("-" * 74)
            print(f"{word}: yellow={len(px.interleaved)}  cards={len(views)}  "
                  f"gradient={gradient}  standalone={len(views) - gradient}  "
                  f"row growth {len(views) - gradient:+d}")
            print(f"  variants  total={variant_rows}  "
                  f"max={max(variant_counts, default=0)}  "
                  f"p90={_pct(variant_counts, 0.90):.0f}  "
                  f"generic-label={generic}")
            print(f"  cognates  total={sum(cognate_counts)}  "
                  f"max={max(cognate_counts, default=0)}  "
                  f"p90={_pct(cognate_counts, 0.90):.0f}")
            print(f"  build_views: {queries} queries, {elapsed*1000:.1f} ms")

            widest = sorted(
                views, key=lambda v: -(v.variant_total + v.cognate_total)
            )[:examples]
            for v in widest:
                print(f"    {v.card.language_code}:{v.card.name.lemma!r} "
                      f"variants={v.variant_total} cognates={v.cognate_total}")
                for x in list(v.variants)[:4]:
                    print(f"        v  {x.language_code}:{x.lemma!r} "
                          f"[{x.relation}]")
                for x in list(v.cognates)[:4]:
                    print(f"        c  {x.language_code}:{x.lemma!r} "
                          f"[{x.relation}]")

            rows.append({
                "cards": len(views), "gradient": gradient,
                "variants": variant_counts, "cognates": cognate_counts,
                "generic": generic, "queries": queries, "ms": elapsed * 1000,
            })
    finally:
        event.remove(engine, "before_cursor_execute", _count)

    if not rows:
        return
    all_variants = [n for r in rows for n in r["variants"]]
    all_cognates = [n for r in rows for n in r["cognates"]]
    print("=" * 74)
    print(f"POOLED over {len(rows)} words, scope={scope}")
    print("=" * 74)
    print(f"  cards           total={sum(r['cards'] for r in rows)}  "
          f"median={statistics.median([r['cards'] for r in rows]):.1f}")
    print(f"  gradient merges total={sum(r['gradient'] for r in rows)}  "
          f"({sum(r['gradient'] for r in rows) / max(sum(r['cards'] for r in rows), 1) * 100:.1f}% of cards)")
    for label, values in (("variants/card", all_variants),
                          ("cognates/card", all_cognates)):
        if values:
            print(f"  {label:<15} median={statistics.median(values):.1f}  "
                  f"p90={_pct(values, 0.90):.0f}  "
                  f"p99={_pct(values, 0.99):.0f}  max={max(values)}  "
                  f"zero={sum(1 for v in values if v == 0)}/{len(values)}")
    print(f"  build_views     queries median="
          f"{statistics.median([r['queries'] for r in rows]):.0f}  "
          f"max={max(r['queries'] for r in rows)}")
    print(f"  build_views     ms median="
          f"{statistics.median([r['ms'] for r in rows]):.1f}  "
          f"max={max(r['ms'] for r in rows):.1f}")

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--scope", default="all", choices=sorted(SCOPES))
    ap.add_argument("--examples", type=int, default=3)
    ap.add_argument("--fanout-only", action="store_true",
                    help="sections 1-3 only: no search, no embedding")
    args = ap.parse_args()

    with SessionLocal() as db:
        db.execute(text("SET lock_timeout = '30s'"))
        db.execute(text("SET idle_in_transaction_session_timeout = '600s'"))
        section_1_2_3(db)
        if not args.fanout_only:
            section_4_5(db, args.scope, args.examples)


if __name__ == "__main__":
    main()
