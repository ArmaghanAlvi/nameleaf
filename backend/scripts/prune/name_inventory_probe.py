"""
Green-card census N1 — name INVENTORY (read-only, DB-side).

For every `name`-POS lexeme already stored, buckets each name sense by NAME
TYPE using category-level predicates only, and reports the orthogonal flags
(gender, diminutive, multiword, romanization) the green-card table will need.

Runs against the DB, not the Kaikki files, deliberately: the import already
dropped Tier A, so initialisms/abbreviations/form-of senses are largely gone
and this measures the rows the feature would actually draw from.

USAGE (from backend/):
  python3 scripts/prune/name_inventory_probe.py --lang la --examples 12
  python3 scripts/prune/name_inventory_probe.py --all
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from collections import Counter

from sqlalchemy import select
from sqlalchemy.orm import selectinload

sys.path.insert(0, os.getcwd())

from app.db.session import SessionLocal              # noqa: E402
from app.models.generated_name import Language       # noqa: E402
from app.models.semantic import Lexeme, Sense        # noqa: E402
from app.utils.languages import LANGUAGE_SCRIPTS     # noqa: E402

# ---------------------------------------------------------------------------
# MOVED to app/services/established_names.py (Breakdown B, Step 5). The rules
# below are re-exported, not reimplemented: the shipped classifier and this
# census must be the SAME code or the published N1 numbers stop describing
# what shipped. Behaviour is unchanged -- this was a cut-and-paste move.
# ---------------------------------------------------------------------------
from app.services.established_names import (           # noqa: E402,F401
    BUCKETS,
    NAME_TYPE_CHOICES,
    SHIPPING_BUCKETS,
    both_given_and_surname,
    classify_name_type,
    expand_type_arg,
    gender_of,
    is_diminutive,
    shipping_types,
)


# ---------------------------------------------------------------------------


def sample_add(bucket: list, item, cap: int) -> None:
    if len(bucket) < cap:
        bucket.append(item)


def run_language(db, lang_id: int, code: str, cap: int, limit: int | None):
    script = LANGUAGE_SCRIPTS.get(code, "?")
    non_latin = script != "Latn"

    buckets: Counter = Counter()
    samples: dict[str, list] = {b: [] for b in BUCKETS}
    cat_shapes: Counter = Counter()
    tag_shapes: Counter = Counter()
    unmatched_prefixes: Counter = Counter()

    # Per-bucket, never blended: GIVEN, SURNAME and PATRONYMIC have different
    # gender profiles by construction, and one averaged triple over all three
    # would be uninterpretable.
    gender: dict[str, Counter] = {b: Counter() for b in SHIPPING_BUCKETS}
    flags: dict[str, Counter] = {b: Counter() for b in SHIPPING_BUCKETS}
    keep_lexemes: dict[str, set[int]] = {b: set() for b in SHIPPING_BUCKETS}
    keep_lemmas: dict[str, set[str]] = {b: set() for b in SHIPPING_BUCKETS}
    romanized: dict[str, set[int]] = {b: set() for b in SHIPPING_BUCKETS}
    seen_lexemes: set[int] = set()

    stmt = (
        select(Sense)
        .join(Lexeme, Lexeme.id == Sense.lexeme_id)
        .options(selectinload(Sense.lexeme))
        .where(
            Lexeme.language_id == lang_id,
            Lexeme.part_of_speech == "name",
        )
        .order_by(Sense.id)
    )

    n = 0
    for sense in db.scalars(stmt).yield_per(2000):
        if limit is not None and n >= limit:
            break
        n += 1
        lex = sense.lexeme
        seen_lexemes.add(lex.id)
        gloss = (sense.definition or "").strip()
        tags = list(sense.raw_tags or [])

        b = classify_name_type(gloss, tags)
        buckets[b] += 1
        sample_add(samples[b], (lex.lemma, gloss[:72]), cap)

        # shape self-report: discover what `categories`/`raw_tags` actually
        # look like in this DB rather than assuming a wiktextract version
        for c in (sense.categories or [])[:6]:
            cat_shapes[str(c)[:60]] += 1
        for t in tags[:6]:
            tag_shapes[str(t)[:40]] += 1

        if b in SHIPPING_BUCKETS:
            keep_lexemes[b].add(lex.id)
            keep_lemmas[b].add(lex.normalized_lemma)
            gender[b][gender_of(gloss, tags)] += 1
            if is_diminutive(gloss):
                flags[b]["diminutive"] += 1
            if both_given_and_surname(gloss):
                flags[b]["given+surname"] += 1
            if " " in (lex.lemma or ""):
                flags[b]["multiword"] += 1
            if getattr(lex, "romanization", None):
                romanized[b].add(lex.id)
        elif b == "OTHER":
            low = gloss.lower()
            if low.startswith(("a ", "an ", "the ")):
                unmatched_prefixes[" ".join(low.split()[:4])] += 1

    total = sum(buckets.values())

    def pct(x, d=total):
        return f"{100 * x / d:.2f}%" if d else "n/a"

    print("=" * 72)
    print(f"LANG {code}  (script {script})   name senses: {total}   "
          f"name lexemes: {len(seen_lexemes)}")
    print("=" * 72)
    for b in BUCKETS:
        print(f"  {b:<12} {buckets[b]:>8} ({pct(buckets[b])})")
        for lemma, g in samples[b]:
            print(f"      {lemma!r}: {g}")
    print()
    for b in SHIPPING_BUCKETS:
        if not buckets[b]:
            print(f"--- SHIPPING SET [{b}] --- (none)")
            continue
        print(f"--- SHIPPING SET [{b}] ---")
        print(f"  senses ......................... {buckets[b]} "
              f"({pct(buckets[b])})")
        print(f"  distinct lexemes ............... {len(keep_lexemes[b])}")
        print(f"  distinct normalized lemmas ..... {len(keep_lemmas[b])}")
        print(f"  gender m/f/x/u ................. "
              f"{gender[b]['m']} / {gender[b]['f']} / "
              f"{gender[b]['x']} / {gender[b]['u']}")
        for k, v in flags[b].most_common():
            print(f"  flag {k:<22} {v}")
        if non_latin:
            print(f"  romanization present ........... {len(romanized[b])} "
                  f"({pct(len(romanized[b]), len(keep_lexemes[b]))} "
                  f"of kept lexemes)")
    print()
    print("--- top raw `categories` values on name senses (shape discovery) ---")
    for c, k in cat_shapes.most_common(12):
        print(f"    {k:>7}  {c!r}")
    print("--- top raw_tags on name senses ---")
    for t, k in tag_shapes.most_common(12):
        print(f"    {k:>7}  {t!r}")
    print("--- top OTHER 'a/an/the ...' prefixes (predicate refinement feed) ---")
    for p, k in unmatched_prefixes.most_common(15):
        print(f"    {k:>7}  {p!r}")
    print()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--lang", action="append", default=[],
                    help="language code; repeatable. Omit with --all.")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--examples", type=int, default=10)
    ap.add_argument("--limit", type=int, default=None,
                    help="cap name senses per language (smoke runs)")
    args = ap.parse_args()

    with SessionLocal() as db:
        rows = db.execute(
            select(Language.id, Language.code).order_by(Language.code)
        ).all()
        wanted = {c for c in args.lang}
        for lang_id, code in rows:
            if not code:
                continue
            if not args.all and wanted and code not in wanted:
                continue
            if not args.all and not wanted:
                continue
            run_language(db, lang_id, code, args.examples, args.limit)


if __name__ == "__main__":
    main()