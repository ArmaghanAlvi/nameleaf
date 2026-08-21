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
# Category-level predicates. Deliberately reuse the vocabulary already proven
# in name_gloss_probe.py so the two censuses stay comparable.
# ---------------------------------------------------------------------------

_PLACE_WORDS = (
    "city|town|village|hamlet|river|mountain|lake|island|state|province|"
    "country|county|region|district|commune|municipality|borough|suburb|"
    "neighborhood|neighbourhood|census-designated place|unincorporated|"
    "placename|place name|place-name|locality|locale|civil parish|parish|"
    "prefecture|ward|settlement|community|ghost town|local government area"
)

_RX = {
    "form_of": re.compile(
        r"^(?:nominative|genitive|dative|accusative|ablative|vocative|"
        r"locative|instrumental|oblique)\b.{0,40}\bof\b"
        r"|^(?:alternative|variant|obsolete|archaic) (?:form|spelling) of\b"
        r"|\b(?:singular|plural|definite|indefinite) of\b"
        r"|^(?:inflection|romanization|transliteration) of\b",
        re.IGNORECASE,
    ),
    "given": re.compile(
        r"\bgiven name\b|\bfirst name\b|\bforename\b"
        r"|^an? (?:male|female|unisex|masculine|feminine) name\b",
        re.IGNORECASE,
    ),
    "surname": re.compile(
        r"\bsurname\b|\bfamily name\b|\blast name\b", re.IGNORECASE,
    ),
    "patronymic": re.compile(r"\b(?:patronymic|matronymic)\b", re.IGNORECASE),
    "place": re.compile(
        rf"^an? (?:[a-z-]+ ){{0,3}}(?:{_PLACE_WORDS})\b"
        rf"|^an? place (?:in|of)\b"
        rf"|^a number of places\b"
        rf"|^the capital (?:city )?of\b"
        rf"|\((?:a|an|the) (?:{_PLACE_WORDS})\b",
        re.IGNORECASE,
    ),
    "diminutive": re.compile(
        r"\b(?:diminutive|pet form|short form|hypocorism|hypocoristic"
        r"|nickname)\b",
        re.IGNORECASE,
    ),
    # Gender is read from the HEAD PHRASE only. A loose \bfemale\b scan is
    # wrong on the systematic de/es shape "a male given name, feminine
    # equivalent Daniela" — both words are present and the loose reading
    # collapses it to unknown.
    "female_head": re.compile(
        r"\b(?:female|feminine)\s+(?:\w+\s+){0,2}?(?:given\s+)?name\b",
        re.IGNORECASE,
    ),
    "male_head": re.compile(
        r"\b(?:male|masculine)\s+(?:\w+\s+){0,2}?(?:given\s+)?name\b",
        re.IGNORECASE,
    ),
    "unisex_head": re.compile(
        r"\b(?:unisex|epicene)\s+(?:\w+\s+){0,2}?(?:given\s+)?name\b",
        re.IGNORECASE,
    ),
}

BUCKETS = ("FORM_OF", "GIVEN", "SURNAME", "PATRONYMIC", "PLACE", "OTHER")


def classify_name_type(gloss: str, tags: list[str] | None = None) -> str:
    """
    Bucket ONE name sense. Priority order is deliberate:

      FORM_OF first  — an inflected/variant citation is not a name row at all,
                       regardless of what it inflects.
      GIVEN, SURNAME — the two shipping buckets; GIVEN wins a tie because
                       "a surname, also a given name" should surface as a
                       given name with the surname flag, not the reverse.
      PATRONYMIC     — only when neither of the above fired, so
                       "a surname, patronymic of X" stays SURNAME.
      PLACE          — toponyms; measured so they can be excluded knowingly.
      OTHER          — deities, organisations, brands, taxonomy, everything
                       else. Expected to be large; that is the point.
    """
    g = (gloss or "").strip()
    tagset = {str(t).strip().lower() for t in (tags or [])}
    if not g:
        return "OTHER"
    if tagset & {"form-of", "alt-of", "alternative"} or _RX["form_of"].search(g):
        return "FORM_OF"
    if _RX["given"].search(g):
        return "GIVEN"
    if _RX["surname"].search(g):
        return "SURNAME"
    if _RX["patronymic"].search(g):
        return "PATRONYMIC"
    if _RX["place"].search(g):
        return "PLACE"
    return "OTHER"


def gender_of(gloss: str, tags: list[str] | None = None) -> str:
    """
    m / f / x (unisex) / u (unknown). Read from the head phrase, and from the
    FIRST such phrase only — "a male given name, feminine equivalent Daniela"
    is a male name, not an ambiguous one.
    """
    tagset = {str(t).strip().lower() for t in (tags or [])}
    if "feminine" in tagset or "female" in tagset:
        return "f"
    if "masculine" in tagset or "male" in tagset:
        return "m"
    g = gloss or ""
    hits = []
    for label, key in (("f", "female_head"), ("m", "male_head"),
                       ("x", "unisex_head")):
        mt = _RX[key].search(g)
        if mt:
            hits.append((mt.start(), label))
    if not hits:
        return "u"
    hits.sort()
    return hits[0][1]


def is_diminutive(gloss: str) -> bool:
    return bool(_RX["diminutive"].search(gloss or ""))


def both_given_and_surname(gloss: str) -> bool:
    g = gloss or ""
    return bool(_RX["given"].search(g)) and bool(_RX["surname"].search(g))


SHIPPING_BUCKETS: tuple[str, ...] = ("GIVEN", "SURNAME", "PATRONYMIC")

_TYPE_ARG_TO_BUCKET: dict[str, tuple[str, ...]] = {
    "given": ("GIVEN",),
    "surname": ("SURNAME",),
    "patronymic": ("PATRONYMIC",),
}

# CLI choices every type-aware probe should use, so a new type is added in
# exactly one place.
NAME_TYPE_CHOICES: tuple[str, ...] = ("given", "surname", "patronymic", "all")


def shipping_types(name_type_arg: str) -> tuple[str, ...]:
    """
    Resolve a --name-type CLI value to the classify_name_type() bucket(s) it
    selects. 'all' is NOT accepted here and never means "combined": callers
    that accept 'all' must loop over expand_type_arg() and keep the reports
    SEPARATE, so the populations are never blended in one count.

    PATRONYMIC added in Stage 1d. Every probe before that silently excluded
    is (71), la (27) and ru (27) -- real populations that no census had seen.
    """
    try:
        return _TYPE_ARG_TO_BUCKET[name_type_arg]
    except KeyError:
        raise ValueError(
            f"shipping_types() takes one of "
            f"{sorted(_TYPE_ARG_TO_BUCKET)}, got {name_type_arg!r}. "
            "For 'all', call expand_type_arg() and keep the reports separate."
        ) from None


def expand_type_arg(name_type_arg: str) -> tuple[str, ...]:
    """
    Expand a --name-type CLI value into the SEQUENCE of single-type args a
    caller should loop over. 'all' now includes patronymic; that is a
    deliberate behaviour change, since 'all' previously meant
    'given + surname' and quietly dropped a third real population.
    """
    if name_type_arg == "all":
        return ("given", "surname", "patronymic")
    return (name_type_arg,)


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