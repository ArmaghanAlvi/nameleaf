"""
Breakdown-B Step 7 -- CATEGORY census (read-only, DB-side).

Stage 3a makes `senses.categories` the primary predicate for name type and
gender, replacing `raw_tags` (grammatical gender, which produced 593m/2,447f
German surnames -- not credible). Before that predicate is written into a
shipping table, four things must be measured, not assumed:

  1. COVERAGE   what share of name senses carry a category the parser
                resolves? Misses fall through to the gloss regexes, so a low
                rate is not fatal -- but it has to be KNOWN.
  2. AGREEMENT  where both fire, do categories and the N1 gloss classifier
                agree? Systematic disagreement means one is wrong, and the
                census that priced this feature was built on it.
  3. PREFIXES   does every language's category prefix equal Language.name?
                If Wiktionary says "Ancient Greek" where the DB says "Greek",
                the parser silently zeroes that language.
  4. MODIFIERS  which words appear between the language name and the type?
                The parser fails CLOSED on unknown ones; this is the list
                that decides what gets admitted to _CATEGORY_MODIFIERS.

Also prices the gender source change -- raw_tags-first (old) vs
categories-then-head-phrase (new) -- per language, with movement samples.

USAGE (from backend/):
  python3 scripts/prune/name_category_census.py --lang de --examples 15
  python3 scripts/prune/name_category_census.py --all
"""
from __future__ import annotations

import argparse
import os
import sys
from collections import Counter

from sqlalchemy import select
from sqlalchemy.orm import selectinload

sys.path.insert(0, os.getcwd())

from app.db.session import SessionLocal                       # noqa: E402
from app.models.generated_name import Language                # noqa: E402
from app.models.semantic import Lexeme, Sense                 # noqa: E402
from app.services.established_names import (                  # noqa: E402
    _CATEGORY_GENDER,
    _CATEGORY_MODIFIERS,
    _CATEGORY_TAIL_RX,
    category_names,
    classify_from_categories,
    classify_name_type,
    classify_sense,
    gender_from_head,
    gender_of,
)

SHIPPING = ("GIVEN", "SURNAME", "PATRONYMIC")


def sample_add(bucket: list, item, cap: int) -> None:
    if len(bucket) < cap:
        bucket.append(item)


def probe_prefix(category: str) -> tuple[str, str] | None:
    """
    Language-AGNOSTIC re-parse, for diagnostics only: find a name-type tail
    anywhere in the string and report the prefix that precedes it. This is
    what turns a Language.name mismatch from a silent zero into a printed
    line.
    """
    words = " ".join((category or "").split()).split()
    for cut in range(1, len(words)):
        m = _CATEGORY_TAIL_RX.match(" ".join(words[cut:]))
        if m:
            return " ".join(words[:cut]), m.group("mods").strip()
    return None


def run_language(db, lang_id: int, code: str, lang_name: str, cap: int,
                 limit: int | None) -> None:
    senses = 0
    form_of = 0
    shipping_final = 0        # final bucket (post classify_sense) in SHIPPING
    resolved_shipping = 0     # of those, the CATEGORY path fired
    fallback_shipping = 0     # of those, gloss fallback fired
    cat_bucket: Counter = Counter()
    gloss_bucket: Counter = Counter()
    agreement: Counter = Counter()
    disagree: list = []
    prefix_seen: Counter = Counter()
    modifier_seen: Counter = Counter()
    unparsed: Counter = Counter()
    gender_old: Counter = Counter()
    gender_new: Counter = Counter()
    gender_moved: Counter = Counter()
    gender_samples: list = []

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

    for sense in db.scalars(stmt).yield_per(2000):
        if limit is not None and senses >= limit:
            break
        senses += 1
        gloss = (sense.definition or "").strip()
        tags = list(sense.raw_tags or [])
        cats = category_names(sense.categories)

        for cat in cats:
            probed = probe_prefix(cat)
            if probed:
                prefix, mods = probed
                prefix_seen[prefix[:40]] += 1
                for word in mods.split():
                    w = word.casefold()
                    if (w not in _CATEGORY_GENDER
                            and w not in _CATEGORY_MODIFIERS):
                        modifier_seen[w] += 1

        g_bucket = classify_name_type(gloss, tags)
        gloss_bucket[g_bucket] += 1

        # FORM_OF gate, matching production: an inflected citation never
        # ships regardless of what categories it inherited.
        if g_bucket == "FORM_OF":
            form_of += 1
            continue

        c_bucket, _c_gender, _also = classify_from_categories(cats, lang_name)
        final_bucket, final_gender, _also2 = classify_sense(
            gloss, tags, sense.categories, lang_name
        )

        if final_bucket not in SHIPPING:
            continue   # PLACE / OTHER -- never part of the shipping count

        shipping_final += 1
        cat_bucket[c_bucket if c_bucket else "<none>"] += 1

        if c_bucket is not None:
            resolved_shipping += 1
            if c_bucket == g_bucket:
                agreement["agree"] += 1
            else:
                agreement[f"cat={c_bucket} gloss={g_bucket}"] += 1
                sample_add(disagree,
                           (sense.lexeme.lemma, c_bucket, g_bucket, gloss[:58]),
                           cap)
        else:
            fallback_shipping += 1
            for cat in cats:
                unparsed[cat[:70]] += 1

        old = gender_of(gloss, tags)
        new = final_gender
        gender_old[old] += 1
        gender_new[new] += 1
        if old != new:
            gender_moved[f"{old}->{new}"] += 1
            sample_add(gender_samples,
                       (sense.lexeme.lemma, old, new, tags[:3], gloss[:48]),
                       cap)

    def pct(x, d=shipping_final):
        return f"{100 * x / d:.2f}%" if d else "n/a"

    print("=" * 72)
    print(f"LANG {code}  (Language.name={lang_name!r})   name senses: {senses}")
    print("=" * 72)
    print(f"  FORM_OF excluded (production never sees these) ... {form_of}")
    print(f"  SHIPPING senses (final bucket, matches classify_sense) "
          f".... {shipping_final}")
    print(f"  resolved via CATEGORY ............... {resolved_shipping} "
          f"({pct(resolved_shipping)})")
    print(f"  fell back to GLOSS ................... {fallback_shipping} "
          f"({pct(fallback_shipping)})")
    print()
    print("--- bucket from CATEGORIES (within shipping population) ---")
    for b in list(SHIPPING) + ["<none>"]:
        print(f"  {b:<12} {cat_bucket[b]:>7}")
    print()
    print("--- AGREEMENT where the category predicate fired ---")
    for k, n in agreement.most_common():
        print(f"  {k:<28} {n:>8}")
    for lemma, cb, gb, g in disagree:
        print(f"      {lemma!r}: cat={cb} gloss={gb}  [{g}]")
    print()
    print("--- GENDER: raw_tags-first (old) vs classify_sense final (new) ---")
    print(f"  old m/f/x/u ... {gender_old['m']} / {gender_old['f']} / "
          f"{gender_old['x']} / {gender_old['u']}")
    print(f"  new m/f/x/u ... {gender_new['m']} / {gender_new['f']} / "
          f"{gender_new['x']} / {gender_new['u']}")
    for k, n in gender_moved.most_common(12):
        print(f"  moved {k:<10} {n:>8}")
    for lemma, o, n2, tg, g in gender_samples:
        print(f"      {lemma!r}: {o}->{n2}  tags={tg}  [{g}]")
    print()
    print("--- LANGUAGE PREFIXES seen before a name-type tail ---")
    print(f"    (Language.name is {lang_name!r}; anything else is an alias "
          f"candidate)")
    for prefix, n in prefix_seen.most_common(10):
        flag = "" if prefix.casefold() == lang_name.casefold() \
            else "   <-- ALIAS?"
        print(f"    {n:>7}  {prefix!r}{flag}")
    print("--- UNKNOWN MODIFIERS (the parser fails closed on these) ---")
    if not modifier_seen:
        print("    (none)")
    for word, n in modifier_seen.most_common(15):
        print(f"    {n:>7}  {word!r}")
    print("--- categories on SHIPPING senses that fell back to gloss ---")
    for cat, n in unparsed.most_common(12):
        print(f"    {n:>7}  {cat!r}")
    print()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--lang", action="append", default=[])
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--examples", type=int, default=10)
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()

    with SessionLocal() as db:
        rows = db.execute(
            select(Language.id, Language.code, Language.name)
            .where(Language.code.isnot(None))
            .order_by(Language.code)
        ).all()
        wanted = set(args.lang)
        for lang_id, code, name in rows:
            if not args.all and code not in wanted:
                continue
            run_language(db, lang_id, code, name, args.examples, args.limit)


if __name__ == "__main__":
    main()