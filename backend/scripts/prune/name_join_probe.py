"""
Green-card census N3 — RETRIEVAL feasibility (read-only, DB-side).

N1 counts the names, N2 counts the meanings. N3 asks whether those meanings
would actually JOIN to the yellow-card result set, and how noisy the join is.
Runs per name TYPE (given or surname, never combined).

Per language:
  * content tokens per meaning — distribution, and how many names yield zero
  * token resolvability — share of tokens that ARE a visible English lexeme
    lemma, i.e. that could ever be produced by an English yellow card
  * top tokens — flood detection ("son", "form", "name" dominating means the
    join returns the same names for every query)
  * method-2 sizing — name lemmas that exactly match a visible non-name lexeme
    in the SAME language, with the most frequent matches shown

Global (with --all):
  * cross-language duplicate name keys — how many normalized name lemmas occur
    in 2+ languages, which decides one-card-per-language vs merged cards
    (computed separately per type)

USAGE (from backend/):
  python3 scripts/prune/name_join_probe.py --name-type given --lang la --examples 12
  python3 scripts/prune/name_join_probe.py --name-type given --all
  python3 scripts/prune/name_join_probe.py --name-type all --all
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
from app.utils.text import normalize_lemma           # noqa: E402
from scripts.prune.name_inventory_probe import (     # noqa: E402
    NAME_TYPE_CHOICES,
    classify_name_type,
    expand_type_arg,
    shipping_types,
)
from scripts.prune.name_meaning_probe import (       # noqa: E402
    extract_equiv_en,
    extract_etym_marker,
    extract_etym_quoted,
    extract_gloss_meaning,
    visible_non_name_lemmas,
)

# Function words plus the boilerplate vocabulary of name glosses/etymologies.
# Not a linguistic stoplist: these are the tokens that, if left in, join to
# everything and make the flood report unreadable.
STOP = frozenset("""
a an the of and or to in from for with by on at as is are was were be been
this that these those it its his her their our your my he she they we you i
also see used use using such more most very not no
name names given surname surnames family first last male female masculine
feminine unisex form forms variant variants spelling diminutive short pet
equivalent english meaning literally lit means derived derivative cognate
son daughter child born place city town river god goddess mythology saint
one two three ancient modern old new common word words letter
""".split())

TOKEN_RX = re.compile(r"[^\W\d_]+", re.UNICODE)


def content_tokens(text: str) -> list[str]:
    return [
        t for t in (m.group(0).lower() for m in TOKEN_RX.finditer(text or ""))
        if len(t) > 2 and t not in STOP
    ]


def best_meaning(gloss: str, etym: str, lang_code: str) -> tuple[str | None, str]:
    """
    N2's waterfall, minus the homograph channel (which carries no text).
    lang_code is required as of Stage 1c: the repaired ETYM_QUOTED gates
    ja/zh off, so the channel is language-dependent.
    """
    v = extract_gloss_meaning(gloss)
    if v:
        return v, "GLOSS_MEANING"
    v = extract_equiv_en(gloss)
    if v:
        return v, "GLOSS_EQUIV_EN"
    v = extract_etym_marker(etym)
    if v:
        return v, "ETYM_MARKER"
    v = extract_etym_quoted(etym, lang_code)
    if v:
        return v, "ETYM_QUOTED"
    return None, "NONE"


def english_lemma_set(db) -> set[str]:
    lang_id = db.scalar(select(Language.id).where(Language.code == "en"))
    if lang_id is None:
        return set()
    rows = db.execute(
        select(Lexeme.normalized_lemma)
        .join(Sense, Sense.lexeme_id == Lexeme.id)
        .where(
            Lexeme.language_id == lang_id,
            Sense.visibility_status == "visible",
        )
        .distinct()
    ).scalars().all()
    return set(rows)


def run_language(db, lang_id: int, code: str, en_lemmas: set[str],
                 cap: int, limit: int | None,
                 wanted_types: tuple[str, ...]) -> set[str]:
    shadow = visible_non_name_lemmas(db, lang_id)

    considered = 0
    with_meaning = 0
    tok_hist: Counter = Counter()
    tokens_total = 0
    tokens_resolved = 0
    top_tokens: Counter = Counter()
    zero_token_samples: list = []
    shadow_hits: Counter = Counter()
    name_keys: set[str] = set()

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
        if limit is not None and considered >= limit:
            break
        gloss = (sense.definition or "").strip()
        tags = list(sense.raw_tags or [])
        if classify_name_type(gloss, tags) not in wanted_types:
            continue
        considered += 1
        lex = sense.lexeme
        name_keys.add(lex.normalized_lemma)

        if lex.normalized_lemma in shadow:
            shadow_hits[lex.lemma] += 1

        meaning, _channel = best_meaning(gloss, sense.etymology_text or "", code)
        if meaning is None:
            tok_hist[0] += 1
            continue
        with_meaning += 1

        toks = content_tokens(meaning)
        tok_hist[min(len(toks), 6)] += 1
        if not toks:
            if len(zero_token_samples) < cap:
                zero_token_samples.append((lex.lemma, meaning[:50]))
        for t in toks:
            tokens_total += 1
            top_tokens[t] += 1
            # normalize through the canonical join key before testing, so this
            # measures the join the feature would actually perform
            if normalize_lemma(t, "en") in en_lemmas:
                tokens_resolved += 1

    def pct(x, d):
        return f"{100 * x / d:.2f}%" if d else "n/a"

    print("=" * 72)
    print(f"LANG {code}   type={'+'.join(wanted_types)}   "
          f"senses: {considered}   distinct name keys: {len(name_keys)}")
    print("=" * 72)
    print(f"  with an extractable meaning .... {with_meaning} "
          f"({pct(with_meaning, considered)})")
    print(f"  content tokens (total) ......... {tokens_total}")
    print(f"  tokens that ARE a visible en lemma "
          f"{tokens_resolved} ({pct(tokens_resolved, tokens_total)})")
    print("  tokens-per-meaning histogram (6 = 6 or more):")
    for k in sorted(tok_hist):
        print(f"      {k}: {tok_hist[k]}")
    if zero_token_samples:
        print("  meanings that tokenized to NOTHING (all stopwords/boilerplate):")
        for lemma, mtext in zero_token_samples:
            print(f"      {lemma!r} -> {mtext!r}")
    print()
    print(f"--- METHOD 2 (name lemma == visible same-language word) ---")
    print(f"  name senses with a same-language word match: "
          f"{sum(shadow_hits.values())} "
          f"({pct(sum(shadow_hits.values()), considered)})")
    print(f"  distinct such names: {len(shadow_hits)}")
    for lemma, k in shadow_hits.most_common(cap):
        print(f"      {k:>4}  {lemma!r}")
    print()
    print("--- TOP MEANING TOKENS (flood detection) ---")
    for t, k in top_tokens.most_common(30):
        mark = "" if normalize_lemma(t, "en") in en_lemmas else "   [no en lexeme]"
        print(f"    {k:>7}  {t!r}{mark}")
    print()
    return name_keys


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--lang", action="append", default=[])
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--examples", type=int, default=10)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument(
        "--name-type", choices=NAME_TYPE_CHOICES, default="given",
        help="'all' runs GIVEN, SURNAME and PATRONYMIC as fully separate "
             "report passes per language -- never combined into one count.",
    )
    args = ap.parse_args()

    type_args = expand_type_arg(args.name_type)

    with SessionLocal() as db:
        en_lemmas = english_lemma_set(db)
        print(f"visible English lemma set: {len(en_lemmas)}\n")

        rows = db.execute(
            select(Language.id, Language.code).order_by(Language.code)
        ).all()
        wanted = set(args.lang)

        for type_arg in type_args:
            wanted_types = shipping_types(type_arg)
            per_lang: dict[str, set[str]] = {}
            for lang_id, code in rows:
                if not code:
                    continue
                if not args.all and code not in wanted:
                    continue
                per_lang[code] = run_language(
                    db, lang_id, code, en_lemmas, args.examples, args.limit,
                    wanted_types,
                )

            if len(per_lang) > 1:
                counts: Counter = Counter()
                for keys in per_lang.values():
                    for k in keys:
                        counts[k] += 1
                shared = {k: v for k, v in counts.items() if v > 1}
                print("=" * 72)
                print(f"CROSS-LANGUAGE NAME KEY OVERLAP   type={type_arg}")
                print("=" * 72)
                print(f"  distinct name keys (all langs) .... {len(counts)}")
                print(f"  keys in 2+ languages .............. {len(shared)} "
                      f"({100 * len(shared) / len(counts):.2f}%)")
                hist: Counter = Counter(shared.values())
                for k in sorted(hist):
                    print(f"      in {k} languages: {hist[k]}")
                print("  most widely shared keys:")
                for k, v in sorted(shared.items(), key=lambda kv: -kv[1])[:20]:
                    print(f"      {v:>3} langs  {k!r}")
                print()


if __name__ == "__main__":
    main()