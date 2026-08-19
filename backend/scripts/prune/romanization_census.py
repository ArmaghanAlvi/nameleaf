"""
D1 -- romanization container census. READ-ONLY, DB-side.

WHY DB-SIDE, NOT FILE-SIDE: Lexeme.raw_entry stores the COMPLETE Kaikki entry
(kaikki_english.py:35, compact_entry_for_storage == dict(entry)), and Lexeme
has exactly one construction site in the repo. So the DB holds every field the
.jsonl.gz held, for exactly the population that survived Tier A/B pruning.
Censusing the files instead would measure the wrong population AND risk the
census diverging from the backfill's extraction logic.

WHY FIVE CONTAINERS, NOT ONE: wiktextract puts romanization in different
places per language. Checking only forms[]/roman (the original roadmap probe)
would report ~0% for zh, where pinyin lives in sounds[], and draw the wrong
conclusion. This reports each container independently.

USAGE (from backend/):
  python3 scripts/prune/romanization_census.py
  python3 scripts/prune/romanization_census.py --limit 20000
  python3 scripts/prune/romanization_census.py --lang zh --dump 12
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter

sys.path.insert(0, os.getcwd())

from sqlalchemy import text                       # noqa: E402
from app.db.session import SessionLocal           # noqa: E402

# Keys on head_templates[].args that carry a transliteration in some editions.
HEAD_ARG_KEYS = ("tr", "rom", "roman", "romanization", "translit", "latin")

# Substrings that mark a sounds[] item as a romanization rather than IPA.
SOUND_HINTS = ("pinyin", "romaji", "romaja", "hepburn", "romanization",
               "romanisation", "transliteration", "revised", "yale",
               "mccune", "jyutping", "wade")

ROMAN_TAGS = ("romanization", "romanisation", "transliteration")

QUERY = text("""
SELECT lx.lemma,
       (lx.raw_entry::jsonb) -> 'forms'          AS forms,
       (lx.raw_entry::jsonb) -> 'head_templates' AS head_templates,
       (lx.raw_entry::jsonb) -> 'sounds'         AS sounds,
       (lx.raw_entry::jsonb) ->> 'roman'         AS top_roman
FROM lexemes lx
WHERE lx.language_id = :lid
  AND EXISTS (SELECT 1 FROM senses s
              WHERE s.lexeme_id = lx.id
                AND s.visibility_status = 'visible')
LIMIT :lim
""")


def probe_forms_tagged(forms) -> str | None:
    for f in forms or []:
        tags = [str(t).lower() for t in (f.get("tags") or [])]
        if any(t in tags for t in ROMAN_TAGS) and f.get("form"):
            return str(f["form"])
    return None


def probe_forms_roman_key(forms) -> str | None:
    for f in forms or []:
        if f.get("roman"):
            return str(f["roman"])
    return None


def probe_head_templates(heads) -> str | None:
    for h in heads or []:
        args = h.get("args") or {}
        for key in HEAD_ARG_KEYS:
            if args.get(key):
                return str(args[key])
    return None


def probe_sounds(sounds) -> str | None:
    for s in sounds or []:
        blob = " ".join(str(t).lower() for t in (s.get("tags") or []))
        keys = {k.lower() for k in s.keys()}
        looks_roman = (
            any(h in blob for h in SOUND_HINTS)
            or any(h in k for k in keys for h in SOUND_HINTS)
        )
        if not looks_roman:
            continue
        for k, v in s.items():
            if k == "tags" or not isinstance(v, str) or not v:
                continue
            if k.lower() in ("ipa", "audio", "ogg_url", "mp3_url", "note"):
                continue
            return v
    return None


PROBES = (
    ("forms_tagged",   lambda r: probe_forms_tagged(r["forms"])),
    ("forms_romankey", lambda r: probe_forms_roman_key(r["forms"])),
    ("head_templates", lambda r: probe_head_templates(r["head_templates"])),
    ("sounds",         lambda r: probe_sounds(r["sounds"])),
    ("top_roman",      lambda r: r["top_roman"] or None),
)


def census(lang_filter: str | None, limit: int, dump: int) -> None:
    with SessionLocal() as db:
        langs = db.execute(text("""
            SELECT id, code, script FROM languages
            WHERE script IS DISTINCT FROM 'Latn' AND code IS NOT NULL
            ORDER BY code
        """)).all()

        if lang_filter:
            langs = [l for l in langs if l.code == lang_filter]

        print(f"{'lang':5s} {'script':6s} {'n':>7s}  " +
              "  ".join(f"{name:>14s}" for name, _ in PROBES) +
              f"  {'UNION':>7s}  {'maxlen':>6s}")

        for lang in langs:
            rows = db.execute(
                QUERY, {"lid": lang.id, "lim": limit}
            ).mappings().all()

            n = len(rows)
            hits: Counter = Counter()
            union = 0
            maxlen = 0
            samples: dict[str, list[tuple[str, str]]] = {
                name: [] for name, _ in PROBES
            }

            for row in rows:
                d = {
                    "forms": row["forms"] or [],
                    "head_templates": row["head_templates"] or [],
                    "sounds": row["sounds"] or [],
                    "top_roman": row["top_roman"],
                }
                found_any = False
                for name, fn in PROBES:
                    try:
                        got = fn(d)
                    except Exception:
                        got = None
                    if got:
                        hits[name] += 1
                        found_any = True
                        maxlen = max(maxlen, len(got))
                        if len(samples[name]) < dump:
                            samples[name].append((row["lemma"], got))
                union += bool(found_any)

            cells = "  ".join(
                f"{hits[name]:6d} {100*hits[name]/max(n,1):5.1f}%"
                for name, _ in PROBES
            )
            print(f"{lang.code:5s} {str(lang.script):6s} {n:7d}  {cells}  "
                  f"{100*union/max(n,1):6.1f}%  {maxlen:6d}")

            if dump:
                for name, _ in PROBES:
                    if samples[name]:
                        pairs = ", ".join(
                            f"{lem}->{val}" for lem, val in samples[name]
                        )
                        print(f"      {name:14s} {pairs}")

        # Raw structure dump for one language, so an unexpected zero can be
        # inspected rather than guessed at.
        if lang_filter and dump:
            print(f"\n--- raw sounds[]/forms[] for first 3 {lang_filter} "
                  f"lexemes ---")
            lid = next(l.id for l in langs)
            for row in db.execute(
                QUERY, {"lid": lid, "lim": 3}
            ).mappings().all():
                print(f"\n  lemma: {row['lemma']}")
                print(f"  forms: {json.dumps(row['forms'], ensure_ascii=False)[:900]}")
                print(f"  sounds: {json.dumps(row['sounds'], ensure_ascii=False)[:900]}")
                print(f"  heads: {json.dumps(row['head_templates'], ensure_ascii=False)[:600]}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--lang", default=None, help="single ISO code")
    ap.add_argument("--limit", type=int, default=20000,
                    help="lexemes per language (0 = all)")
    ap.add_argument("--dump", type=int, default=4,
                    help="sample pairs per container")
    args = ap.parse_args()
    census(args.lang, args.limit if args.limit else 10_000_000, args.dump)


if __name__ == "__main__":
    main()