"""
Stage 14a -- ORIGIN and LANGUAGE-HEADER census (read-only).

THE QUESTION. English Wiktionary genuinely files `Nadiya`, `Amshuman`,
`Jyothis` as English proper nouns, so the importer is not wrong to store
them with language_id = en. What it discards is WHERE they came from.
Findings 19.4 falsified the simple version of that (F-2): zero of the four
sampled rows carry a `from <Lang>` tail. The real signals are the CONNECTOR
shape and a Wiktionary MAINTENANCE category, and nobody has counted either.

FOUR COUNTS PER LANGUAGE, plus the cross-tab that decides the policy:
  (i)   `from <X>` tail          -- _CATEGORY_TAIL_RX's discarded group
  (ii)  connector source run     -- "<Lang> renderings of <Src> ..."
  (iii) `<Lang> entries with incorrect language header`  (maintenance flag)
  (iv)  a BARE `<Lang> given names` with no qualifier at all

  CROSS-TAB (iii) x (iv). If maintenance-flagged rows almost always ALSO
  carry a bare category (as Amshuman and Jyothis do), the flag alone
  identifies them and exclusion is a one-predicate change. If they mostly
  do not (as Nadiya does not), the two signals describe DIFFERENT
  populations and need separate handling. That is the whole reason this
  census exists rather than a schema change on a hunch.

GRAIN. Mirrors populate_established_names.collect_language: same sense
query, same classify_sense, same (normalized_lemma, name_type) grouping,
reducing across every sense in the group. A census over source_sense_id
alone would undercount, because the origin can sit on a sibling sense that
lost the MEANING waterfall.

READ-ONLY. Nothing here writes; the shipped regexes are NOT edited -- the
two below are local mirrors with the origin run NAMED instead of discarded.

USAGE (from backend/):
  python3 scripts/prune/name_origin_census.py --lang en --examples 15
  python3 scripts/prune/name_origin_census.py --all
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from collections import Counter

sys.path.insert(0, os.getcwd())

from sqlalchemy import select                                    # noqa: E402
from sqlalchemy.orm import selectinload                          # noqa: E402

from app.db.session import SessionLocal                          # noqa: E402
from app.models.generated_name import Language                   # noqa: E402
from app.models.semantic import Lexeme, Sense                    # noqa: E402
from app.services.established_names import (                     # noqa: E402
    _CATEGORY_CONNECTORS,
    _CATEGORY_GENDER,
    _CATEGORY_MODIFIERS,
    _TYPE_ALTERNATION,
    category_names,
    classify_sense,
)

BUCKET_TO_TYPE = {"GIVEN": "given", "SURNAME": "surname",
                  "PATRONYMIC": "patronymic"}
BATCH = 2000

_TAIL_WITH_ORIGIN_RX = re.compile(
    r"^(?P<mods>(?:[\w'\u2019-]+\s+)*?)"
    r"(?P<type>" + _TYPE_ALTERNATION + r")"
    r"(?:\s+(?P<kw>from|of|in|derived\s+from)\b(?P<tail>.*))?$",
    re.IGNORECASE,
)
_OPEN_WITH_ORIGIN_RX = re.compile(
    r"^(?P<origin>(?:[\w'\u2019-]+\s+)*?)"
    r"(?:(?P<gender>male|female|unisex|masculine|feminine|epicene)\s+)?"
    r"(?P<type>" + _TYPE_ALTERNATION + r")$",
    re.IGNORECASE,
)

# A capitalized 1-4 word run. Language names in Wiktionary categories are
# capitalized ("Ukrainian", "Ancient Greek", "Old Church Slavonic");
# modifiers and type words are not ("possessive", "patronymics"). That is
# the whole test -- NOT a list of languages, so language #22 self-classifies
# exactly as the type parser already does.
_ORIGIN_WORD_RX = re.compile(r"^[A-Z][\w'\u2019-]*$")
MAX_ORIGIN_WORDS = 4


def origin_run(text: str) -> str | None:
    words = (text or "").split()
    if not words or len(words) > MAX_ORIGIN_WORDS:
        return None
    if not all(_ORIGIN_WORD_RX.match(w) for w in words):
        return None
    return " ".join(words)


def category_origin(category: str, language_name: str):
    """ONE category -> (shape, value) or None when not a name category here.

    shape is one of:
      'bare'                -- resolves, states no origin
      'from' / 'rendering'  -- resolves, origin captured
      'from?' / 'rendering?'-- resolves, origin run present but UNPARSED
                               (reported so the filter can be judged, the
                               same way name_category_census reports
                               modifier candidates)
    """
    text = " ".join((category or "").split())
    lowered = text.casefold()
    prefix = (language_name or "").casefold()
    if not prefix or not lowered.startswith(prefix + " "):
        return None
    rest = text[len(prefix) + 1:]
    rest_lower = rest.casefold()

    for connector in _CATEGORY_CONNECTORS:
        if rest_lower.startswith(connector):
            m = _OPEN_WITH_ORIGIN_RX.match(rest[len(connector):])
            if not m:
                return None
            run = (m.group("origin") or "").strip()
            parsed = origin_run(run)
            if parsed:
                return "rendering", parsed
            return ("rendering?", run) if run else ("bare", None)

    m = _TAIL_WITH_ORIGIN_RX.match(rest)
    if not m:
        return None
    for word in m.group("mods").split():
        w = word.casefold()
        if w not in _CATEGORY_GENDER and w not in _CATEGORY_MODIFIERS:
            return None
    kw = " ".join((m.group("kw") or "").split()).casefold()
    if kw not in ("from", "derived from"):
        return "bare", None
    tail = (m.group("tail") or "").strip()
    parsed = origin_run(tail)
    if parsed:
        return "from", parsed
    return ("from?", tail) if tail else ("bare", None)


def run_language(db, lang, examples: int) -> Counter:
    warn_cat = f"{lang.name} entries with incorrect language header"
    groups: dict[tuple[str, str], dict] = {}

    stmt = (
        select(Sense)
        .join(Lexeme, Lexeme.id == Sense.lexeme_id)
        .options(selectinload(Sense.lexeme))
        .where(Lexeme.language_id == lang.id,
               Lexeme.part_of_speech == "name")
        .order_by(Sense.id)
    )
    for sense in db.scalars(stmt).yield_per(BATCH):
        lex = sense.lexeme
        gloss = (sense.definition or "").strip()
        bucket, _g, _a = classify_sense(
            gloss, list(sense.raw_tags or []), sense.categories, lang.name)
        if bucket not in BUCKET_TO_TYPE:
            continue
        key = (lex.normalized_lemma, BUCKET_TO_TYPE[bucket])
        g = groups.setdefault(key, {
            "lemma": lex.lemma, "from": None, "rendering": None,
            "bare": False, "warn": False, "unparsed": None,
        })
        names = category_names(sense.categories)
        if warn_cat in names:
            g["warn"] = True
        for cat in names:
            hit = category_origin(cat, lang.name)
            if hit is None:
                continue
            shape, value = hit
            if shape == "bare":
                g["bare"] = True
            elif shape in ("from", "rendering"):
                if g[shape] is None:
                    g[shape] = value
            elif g["unparsed"] is None:
                g["unparsed"] = f"{shape} {value!r} <- {cat!r}"

    stats: Counter = Counter()
    origins: Counter = Counter()
    unparsed: list[str] = []
    warn_and_bare: list[str] = []
    warn_only: list[str] = []
    for g in groups.values():
        stats["rows"] += 1
        # Precedence: the connector shape is the more specific claim.
        shape = "rendering" if g["rendering"] else ("from" if g["from"]
                                                    else None)
        if shape:
            stats[shape] += 1
            origins[f"{shape}:{g[shape]}"] += 1
        elif g["bare"]:
            stats["bare_only"] += 1
        else:
            stats["unresolved"] += 1
        if g["warn"]:
            stats["warn"] += 1
            if shape:
                stats["warn_with_origin"] += 1
            elif g["bare"]:
                stats["warn_and_bare"] += 1
                if len(warn_and_bare) < examples:
                    warn_and_bare.append(g["lemma"])
            else:
                stats["warn_only"] += 1
                if len(warn_only) < examples:
                    warn_only.append(g["lemma"])
        if g["unparsed"] and len(unparsed) < examples:
            unparsed.append(f"{g['lemma']}: {g['unparsed']}")

    print(f"\n=== {lang.code} ({lang.name}) ===")
    print(f"  rows ................. {stats['rows']}")
    print(f"  origin 'from' ........ {stats['from']}")
    print(f"  origin 'rendering' ... {stats['rendering']}")
    print(f"  bare only ............ {stats['bare_only']}")
    print(f"  unresolved ........... {stats['unresolved']}")
    print(f"  header WARNING ....... {stats['warn']}")
    print(f"    ...with an origin .. {stats['warn_with_origin']}")
    print(f"    ...also bare ....... {stats['warn_and_bare']}   "
          f"<- the cross-tab")
    print(f"    ...neither ......... {stats['warn_only']}")
    if origins:
        print("  top origins:")
        for label, n in origins.most_common(10):
            print(f"    {n:>6}  {label}")
    for label, bucket in (("warn+bare samples", warn_and_bare),
                          ("warn-only samples", warn_only),
                          ("UNPARSED origin runs", unparsed)):
        if bucket:
            print(f"  {label}:")
            for item in bucket:
                print(f"    {item}")
    return stats


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--lang", default=None, help="comma-separated ISO codes")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--examples", type=int, default=10)
    args = ap.parse_args()

    codes = args.lang.split(",") if args.lang else None
    with SessionLocal() as db:
        rows = db.execute(
            select(Language.id, Language.code, Language.name)
            .where(Language.code.isnot(None)).order_by(Language.code)
        ).all()
        if codes:
            rows = [r for r in rows if r.code in set(codes)]
        elif not args.all:
            ap.error("pass --lang or --all")

        grand: Counter = Counter()
        for lang in rows:
            grand.update(run_language(db, lang, args.examples))

    print("\n" + "=" * 70)
    print("GRAND TOTAL")
    print("=" * 70)
    for key in ("rows", "from", "rendering", "bare_only", "unresolved",
                "warn", "warn_with_origin", "warn_and_bare", "warn_only"):
        print(f"  {key:<20} {grand[key]:>8}")


if __name__ == "__main__":
    main()