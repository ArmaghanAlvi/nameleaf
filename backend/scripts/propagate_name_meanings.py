"""
Stage 6 -- convert Stage-3 blanks into inherited meanings or honest labels.

RUN ORDER (see scripts/build_name_graph.py for the full chain):
  populate_established_names.py -> build_name_graph.py -> THIS ->
  populate_established_names.py --pass tokens

PASSES:
  clear       reset every inherited meaning to NULL (idempotence guarantee)
  equiv       6b -- propagate along EQUIV_EN edges
  homograph   6a -- set homograph_confidence on every linked row, and
              inherit the word's gloss where the name is still blank
  all         clear -> equiv -> homograph
  report      coverage and channel mix, writes nothing

TWO RULES THAT KEEP THIS HONEST
1. SINGLE HOP, DERIVED SOURCES ONLY. A meaning is only propagated from a row
   whose own channel is GLOSS_MEANING / ETYM_MARKER / ETYM_QUOTED -- never
   from a row that itself inherited. That makes the two passes
   order-independent, makes a re-run byte-identical, and stops a chain of
   equivalences from carrying a Latin gloss into Icelandic via three hops.
   It is the same non-transitivity Stage 5b applied to the graph.
2. NEVER OVERWRITE. Both passes only touch rows where meaning_text IS NULL.
   Stage 3's derived meanings outrank anything inherited, by construction.

WHY HOMOGRAPHS GET A CONFIDENCE LEVEL. The roadmap's 6a assumed the name IS
the word, so inheriting the gloss "isn't a guess." IMPORT_PREP_FINDINGS.md
section 5.1 disproves that with a worked example already in this corpus:
`Lucius` (name) shares its key with `lucius` ("a fish, probably the pike"),
but descends from *lux*. 12,598 rows carry a homograph link, so shipping all
of them as flat assertions would ship falsehoods at scale. Rows whose own
etymology names the homograph lemma are `corroborated`; the rest are
`spelling_only` and are labelled as a spelling coincidence, not a meaning.

USAGE (from backend/):
  python3 scripts/propagate_name_meanings.py --report
  python3 scripts/propagate_name_meanings.py --dry-run
  python3 scripts/propagate_name_meanings.py
  python3 scripts/propagate_name_meanings.py --pass homograph
"""
from __future__ import annotations

import argparse
import os
import sys
from collections import Counter

sys.path.insert(0, os.getcwd())

from sqlalchemy import text                                      # noqa: E402
from sqlalchemy.orm import Session                               # noqa: E402

from app.db.session import SessionLocal                          # noqa: E402
from app.services.established_names import (                     # noqa: E402
    MAX_MEANING_LEN,
    homograph_confidence,
)

BATCH = 2000


def clear_inherited(db: Session, dry_run: bool) -> int:
    n = db.execute(text("""
        SELECT count(*) FROM established_names
        WHERE meaning_channel IN ('HOMOGRAPH', 'EQUIV_PROPAGATED')
    """)).scalar_one()
    if dry_run:
        return int(n)
    # meaning_source_name_id must clear in the SAME statement:
    # ck_established_names_meaning_source_channel forbids it surviving a
    # channel change.
    db.execute(text("""
        UPDATE established_names
        SET meaning_text = NULL,
            meaning_channel = NULL,
            meaning_source_name_id = NULL
        WHERE meaning_channel IN ('HOMOGRAPH', 'EQUIV_PROPAGATED')
    """))
    db.execute(text(
        "UPDATE established_names SET homograph_confidence = NULL"
    ))
    db.commit()
    return int(n)


def propagate_equivalence(db: Session, dry_run: bool) -> Counter:
    """
    6b. Blank name --EQUIV_EN--> a name with a DERIVED meaning: copy it.

    Ties break on the lowest target id so a rebuild is deterministic. The
    source row id is stored in meaning_source_name_id: `equiv_en_target` is
    the raw extracted STRING, and the row it actually resolved to is a
    different fact worth keeping -- Stage 10 samples it and Stage 8's label
    needs it.
    """
    rows = db.execute(text("""
        SELECT DISTINCT ON (e.source_name_id)
               e.source_name_id AS blank_id,
               e.target_name_id AS src_id,
               t.meaning_text   AS meaning
        FROM established_name_edges e
        JOIN established_names b ON b.id = e.source_name_id
        JOIN established_names t ON t.id = e.target_name_id
        WHERE e.relation_type = 'EQUIV_EN'
          AND b.meaning_text IS NULL
          AND t.meaning_text IS NOT NULL
          AND t.meaning_channel IN
              ('GLOSS_MEANING', 'ETYM_MARKER', 'ETYM_QUOTED')
          AND t.id <> b.id
        ORDER BY e.source_name_id, e.target_name_id
    """)).mappings().all()

    stats: Counter = Counter()
    payload = []
    for r in rows:
        meaning = (r["meaning"] or "").strip()
        if not meaning or len(meaning) > MAX_MEANING_LEN:
            stats["skipped_bad_meaning"] += 1
            continue
        payload.append({"id": r["blank_id"], "m": meaning,
                        "src": r["src_id"]})
    stats["filled"] = len(payload)
    if dry_run:
        return stats

    update = text("""
        UPDATE established_names
        SET meaning_text = :m,
            meaning_channel = 'EQUIV_PROPAGATED',
            meaning_source_name_id = :src
        WHERE id = :id AND meaning_text IS NULL
    """)
    for start in range(0, len(payload), BATCH):
        db.execute(update, payload[start:start + BATCH])
    db.commit()
    return stats


def inherit_homographs(db: Session, dry_run: bool) -> Counter:
    """
    6a. Two things, in one pass over the linked rows:

      * set `homograph_confidence` on EVERY linked row, blank or not -- a
        card retrieved by mechanism 2 needs the label even when Stage 3
        already gave it a meaning of its own;
      * where the row is still blank, inherit the homograph's gloss.

    The gloss is the homograph lexeme's FIRST VISIBLE sense in dictionary
    order (source_order, sense_index, id). Deterministic and cheap. It is
    not "the best" sense -- picking that would need the query context, which
    does not exist at build time -- so the label never claims more than
    "this is what that word's entry leads with".
    """
    rows = db.execute(text("""
        SELECT en.id                AS name_id,
               en.meaning_text      AS existing,
               lx.lemma             AS word,
               l.code               AS code,
               s.etymology_text     AS name_etym,
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
        ORDER BY en.id
    """)).mappings().all()

    stats: Counter = Counter()
    conf_payload = []
    fill_payload = []
    for r in rows:
        level = homograph_confidence(r["name_etym"], r["word"], r["code"])
        stats[f"conf_{level}"] += 1
        conf_payload.append({"id": r["name_id"], "c": level})
        if r["existing"] is not None:
            stats["already_had_a_meaning"] += 1
            continue
        gloss = " ".join((r["word_gloss"] or "").split()).strip()
        if not gloss:
            stats["word_gloss_missing"] += 1
            continue
        if len(gloss) > MAX_MEANING_LEN:
            gloss = gloss[:MAX_MEANING_LEN].rsplit(" ", 1)[0]
            stats["gloss_truncated"] += 1
        fill_payload.append({"id": r["name_id"], "m": gloss})
        stats[f"filled_{level}"] += 1
    stats["linked_rows"] = len(rows)
    stats["filled"] = len(fill_payload)
    if dry_run:
        return stats

    conf_sql = text(
        "UPDATE established_names SET homograph_confidence = :c "
        "WHERE id = :id"
    )
    fill_sql = text("""
        UPDATE established_names
        SET meaning_text = :m, meaning_channel = 'HOMOGRAPH'
        WHERE id = :id AND meaning_text IS NULL
    """)
    for start in range(0, len(conf_payload), BATCH):
        db.execute(conf_sql, conf_payload[start:start + BATCH])
    for start in range(0, len(fill_payload), BATCH):
        db.execute(fill_sql, fill_payload[start:start + BATCH])
    db.commit()
    return stats


def report(db: Session) -> None:
    total = db.execute(
        text("SELECT count(*) FROM established_names")
    ).scalar_one()
    chan = db.execute(text("""
        SELECT coalesce(meaning_channel, '<blank>') AS ch, count(*) AS n
        FROM established_names GROUP BY 1 ORDER BY 2 DESC
    """)).mappings().all()
    print(f"total rows: {total}")
    print("--- meaning_channel ---")
    covered = 0
    for r in chan:
        if r["ch"] != "<blank>":
            covered += r["n"]
        print(f"  {r['ch']:<18} {r['n']:>8}  "
              f"({100 * r['n'] / max(total, 1):5.2f}%)")
    print(f"  COVERAGE           {covered:>8}  "
          f"({100 * covered / max(total, 1):5.2f}%)")

    conf = db.execute(text("""
        SELECT coalesce(homograph_confidence, '<unlinked>') AS c,
               count(*) AS n
        FROM established_names GROUP BY 1 ORDER BY 2 DESC
    """)).mappings().all()
    print("\n--- homograph_confidence ---")
    for r in conf:
        print(f"  {r['c']:<18} {r['n']:>8}")

    per_lang = db.execute(text("""
        SELECT l.code,
               count(*) AS rows,
               count(en.meaning_text) AS meaning,
               count(*) FILTER (WHERE en.meaning_channel = 'HOMOGRAPH')
                   AS homograph,
               count(*) FILTER (WHERE en.meaning_channel
                   = 'EQUIV_PROPAGATED') AS equiv
        FROM established_names en
        JOIN languages l ON l.id = en.language_id
        GROUP BY l.code ORDER BY l.code
    """)).mappings().all()
    print("\n--- per language ---")
    print(f"{'lang':6s}{'rows':>8s}{'meaning':>9s}{'cover':>8s}"
          f"{'homogr':>8s}{'equiv':>7s}")
    for r in per_lang:
        n = max(r["rows"], 1)
        print(f"{r['code']:6s}{r['rows']:8d}{r['meaning']:9d}"
              f"{100 * r['meaning'] / n:7.1f}%{r['homograph']:8d}"
              f"{r['equiv']:7d}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pass", dest="which", default="all",
                    choices=["all", "clear", "equiv", "homograph"])
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--report", action="store_true")
    args = ap.parse_args()

    with SessionLocal() as db:
        db.execute(text("SET lock_timeout = '30s'"))
        if args.report:
            report(db)
            return
        if args.which in ("all", "clear"):
            n = clear_inherited(db, args.dry_run)
            print(f"cleared inherited meanings: {n}")
        if args.which in ("all", "equiv"):
            s = propagate_equivalence(db, args.dry_run)
            print(f"6b equivalence filled: {s['filled']}  "
                  f"(skipped {s['skipped_bad_meaning']})")
        if args.which in ("all", "homograph"):
            s = inherit_homographs(db, args.dry_run)
            print(f"6a homograph linked rows: {s['linked_rows']}")
            print(f"   corroborated={s['conf_corroborated']}  "
                  f"spelling_only={s['conf_spelling_only']}")
            print(f"   filled={s['filled']} "
                  f"(corroborated={s['filled_corroborated']}, "
                  f"spelling_only={s['filled_spelling_only']})")
            print(f"   already had a meaning={s['already_had_a_meaning']}  "
                  f"word gloss missing={s['word_gloss_missing']}")


if __name__ == "__main__":
    main()