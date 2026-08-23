"""
Breakdown-C Step 7 -- HOMOGRAPH COHERENCE probe (read-only, DB-side).

THE QUESTION. Stage 3c linked 12,598 established names to a same-language
non-name lexeme sharing their canonical key. Roadmap 6a proposes inheriting
that word's gloss as the name's meaning, on the grounds that "the name IS
that word." IMPORT_PREP_FINDINGS.md section 5.1 already records a
counter-example from this corpus:

    aurora (noun) "dawn, sunrise"   / Aurora (name) -- the name IS the word
    lucius (noun) "a fish, ... pike" / Lucius (name) -- from *lux*, NOT the fish

Sharing a key is not sharing a meaning. Before 12,598 glosses are written
into a shipping table, the split has to be measured rather than assumed.

THE SIGNAL. `homograph_confidence()` in app/services/established_names.py --
the SAME function the Stage-6 pass uses, so this probe measures the thing
that ships. It asks one derivable question: does the NAME's own
`etymology_text` name the homograph's lemma? If yes the connection is
attested by Wiktionary itself (`corroborated`); if not, all we know is that
the spellings match (`spelling_only`).

WHAT THE ANSWER DECIDES
  * high corroboration  -> 6a is a MEANING feature; corroborated rows may
                           assert, and their tokens may join in mechanism 1
  * low corroboration   -> 6a is mostly a LABEL feature; the roadmap's
                           "closes a large share of blanks" is retracted and
                           spelling_only rows ship hedged and untokenized

The probe deliberately does NOT propose a better signal. If corroboration is
thin, the next move is another measurement, not another mechanism.

USAGE (from backend/):
  python3 scripts/prune/homograph_coherence_probe.py --all --examples 12
  python3 scripts/prune/homograph_coherence_probe.py --lang hi --lang la
"""
from __future__ import annotations

import argparse
import os
import sys
from collections import Counter

sys.path.insert(0, os.getcwd())

from sqlalchemy import String, bindparam, text                           # noqa: E402

from app.db.session import SessionLocal                          # noqa: E402
from app.services.established_names import (                     # noqa: E402
    content_tokens,
    homograph_confidence,
)

SQL = """
    SELECT en.id, en.lemma, en.name_type, en.meaning_text,
           lx.lemma AS word, lx.part_of_speech AS word_pos,
           l.code AS code, s.etymology_text AS name_etym,
           (
             SELECT s2.definition FROM senses s2
             WHERE s2.lexeme_id = en.homograph_lexeme_id
               AND s2.visibility_status = 'visible'
             ORDER BY s2.source_order, s2.sense_index, s2.id
             LIMIT 1
           ) AS word_gloss
    FROM established_names en
    JOIN lexemes lx  ON lx.id = en.homograph_lexeme_id
    JOIN languages l ON l.id = en.language_id
    JOIN senses s    ON s.id = en.source_sense_id
    WHERE (:code IS NULL OR l.code = :code)
    ORDER BY en.id
"""


def sample_add(bucket: list, item, cap: int) -> None:
    if len(bucket) < cap:
        bucket.append(item)


def run(db, code: str | None, cap: int) -> None:
    stmt = text(SQL).bindparams(bindparam("code", type_=String))
    rows = db.execute(stmt, {"code": code}).mappings().all()
    if not rows:
        return

    conf: Counter = Counter()
    by_lang: dict[str, Counter] = {}
    pos: Counter = Counter()
    blank_conf: Counter = Counter()
    gloss_missing = 0
    long_gloss = 0
    token_load = 0
    samples: dict[str, list] = {"corroborated": [], "spelling_only": []}

    for r in rows:
        level = homograph_confidence(r["name_etym"], r["word"], r["code"])
        conf[level] += 1
        by_lang.setdefault(r["code"], Counter())[level] += 1
        pos[f"{level}:{r['word_pos']}"] += 1
        gloss = " ".join((r["word_gloss"] or "").split())
        if not gloss:
            gloss_missing += 1
        if len(gloss) > 200:
            long_gloss += 1
        if r["meaning_text"] is None:
            blank_conf[level] += 1
            token_load += min(len(content_tokens(gloss)), 12)
        sample_add(
            samples[level],
            (f"{r['code']}:{r['lemma']}", r["word"], gloss[:56],
             (r["name_etym"] or "")[:56]),
            cap,
        )

    total = len(rows)

    def pct(x):
        return f"{100 * x / max(total, 1):.2f}%"

    print("=" * 74)
    print(f"HOMOGRAPH COHERENCE  lang={code or 'ALL'}   linked rows: {total}")
    print("=" * 74)
    print(f"  corroborated ................... {conf['corroborated']} "
          f"({pct(conf['corroborated'])})")
    print(f"  spelling_only .................. {conf['spelling_only']} "
          f"({pct(conf['spelling_only'])})")
    print(f"  homograph word has no visible gloss ... {gloss_missing}")
    print(f"  word gloss over 200 chars ............. {long_gloss}")
    print()
    print("--- rows STILL BLANK after Stage 3 (what 6a would fill) ---")
    print(f"  corroborated ... {blank_conf['corroborated']}")
    print(f"  spelling_only .. {blank_conf['spelling_only']}")
    print(f"  tokens these would add to the mechanism-1 join surface: "
          f"{token_load}")
    print()
    print("--- word part-of-speech by confidence ---")
    for k, n in pos.most_common(14):
        print(f"    {n:>7}  {k}")
    print()
    print("--- per language ---")
    print(f"    {'lang':6s}{'linked':>8s}{'corrob':>8s}{'rate':>8s}")
    for lang in sorted(by_lang):
        c = by_lang[lang]
        n = c["corroborated"] + c["spelling_only"]
        print(f"    {lang:6s}{n:8d}{c['corroborated']:8d}"
              f"{100 * c['corroborated'] / max(n, 1):7.1f}%")
    print()
    for level in ("corroborated", "spelling_only"):
        print(f"--- samples [{level}]  (name | word | word gloss) ---")
        for name, word, gloss, etym in samples[level]:
            print(f"    {name!r} | {word!r} | {gloss!r}")
            print(f"        etym: {etym!r}")
        print()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--lang", action="append", default=[])
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--examples", type=int, default=10)
    args = ap.parse_args()

    with SessionLocal() as db:
        if args.all:
            run(db, None, args.examples)
        for code in args.lang:
            run(db, code, args.examples)


if __name__ == "__main__":
    main()