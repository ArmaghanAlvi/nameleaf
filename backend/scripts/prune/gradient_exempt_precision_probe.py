"""10d, scoped to the population Stage 18c made load-bearing.

WHY THIS EXISTS SEPARATELY FROM homograph_coherence_probe.py. That probe
measures whether etymology_mentions() corroborates a homograph link -- a
mechanism health check across all languages. This one asks a narrower
question that only became askable after Breakdown J: of the rows
apply_origin exempts from the origin pass BECAUSE they are homographs, on
what share is "same-language native" actually the right answer?

A false homograph used to cost a card an overclaimed meaning, which 11d
already hedged. It now costs a silent, untested native-origin assertion on
a row that never entered the LLM pass at all.

READ-ONLY. Emits a stratified sample with the evidence needed to read each
row -- the name, the word's gloss, the name's own etymology, and the
corroboration verdict -- so the precision read is a measurement of the
mechanism, not a curation pass over the data.

USAGE (from backend/):
  python3 scripts/prune/gradient_exempt_precision_probe.py --n 50
  python3 scripts/prune/gradient_exempt_precision_probe.py --n 50 --seed 7
"""
import argparse
import os
import sys

sys.path.insert(0, os.getcwd())

from sqlalchemy import text                            # noqa: E402
from app.db.session import SessionLocal                # noqa: E402

SQL = text("""
SELECT en.lemma, en.name_type, en.gender,
       en.meaning_text, en.meaning_channel,
       en.homograph_confidence,
       lx.lemma AS word_lemma,
       ws.definition AS word_gloss,
       ns.etymology_text AS name_etym
FROM established_names en
JOIN languages l ON l.id = en.language_id
LEFT JOIN lexemes lx ON lx.id = en.homograph_lexeme_id
LEFT JOIN senses ws ON ws.lexeme_id = lx.id
                   AND ws.visibility_status = 'visible'
LEFT JOIN senses ns ON ns.id = en.source_sense_id
WHERE l.code = 'en'
  AND en.origin_source = 'gradient_exempt'
ORDER BY md5(en.lemma || en.name_type || :seed)
LIMIT :n
""")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--n", type=int, default=50)
    ap.add_argument("--seed", default="k")
    args = ap.parse_args()

    with SessionLocal() as db:
        total = db.scalar(text("""
            SELECT count(*) FROM established_names en
            JOIN languages l ON l.id = en.language_id
            WHERE l.code = 'en' AND en.origin_source = 'gradient_exempt'
        """))
        by_conf = db.execute(text("""
            SELECT en.homograph_confidence, count(*) AS rows
            FROM established_names en
            JOIN languages l ON l.id = en.language_id
            WHERE l.code = 'en' AND en.origin_source = 'gradient_exempt'
            GROUP BY 1 ORDER BY 2 DESC
        """)).all()

        print(f"gradient_exempt population (en): {total}")
        for conf, n in by_conf:
            print(f"  {str(conf):<16} {n}")
        print(f"\n--- random sample, n={args.n}, seed={args.seed!r} ---\n")

        for i, r in enumerate(db.execute(
                SQL, {"n": args.n, "seed": args.seed}).mappings(), 1):
            print(f"{i:>3}. {r['lemma']} [{r['name_type']}/{r['gender']}] "
                  f"conf={r['homograph_confidence']}")
            print(f"     word:  {r['word_lemma']} -- "
                  f"{(r['word_gloss'] or '')[:110]}")
            print(f"     name:  {(r['name_etym'] or '(no etymology)')[:110]}")
            print(f"     shown: {(r['meaning_text'] or '(blank)')[:80]} "
                  f"[{r['meaning_channel']}]")
            print()


if __name__ == "__main__":
    main()