"""
Stage 3 -- populate `established_names` and `established_name_tokens` from
the Kaikki name senses already in Postgres.

DB-SIDE ONLY. No corpus file is read: the 21-language import already stored
every name sense, and `Lexeme.romanization` was already derived in Phase D.
Reading the DB rather than the files is what keeps this script and the
N1/N2/N3 census describing the same population.

GRAIN: one row per (language, normalized_lemma, name_type). Classification is
per SENSE -- a lemma with a given-name sense AND a surname sense gets two
rows, one in each type, which is what Stage 5e's "two graphs, never unioned"
requires. Several senses landing in the SAME bucket collapse into one row;
`source_sense_id` points at the sense that won the meaning waterfall, so
provenance stays exact.

REBUILD, NOT UPSERT. Every row in these tables is derived, so `--lang de`
deletes de's rows and re-derives them inside one transaction. That is
simplest and fully deterministic.
  WARNING for Stage 5 onwards: established_name_edges FKs into
  established_names with ON DELETE CASCADE, so a rebuild also drops that
  language's edges and orphans its clusters. Once Stage 5 lands, a rebuild
  must be followed by a re-run of the edge/cluster builders.

PASSES (all idempotent, all re-runnable independently):
  names      classify, group, derive meaning/equivalence/romanization, insert
  homograph  set homograph_lexeme_id  (mechanism 2, Stage 3c)
  tokens     rebuild established_name_tokens (mechanism 1 join surface)
  all        names -> homograph -> tokens

  ⚠ Stage 6 rewrites meaning_text (homograph inheritance, equivalence
  propagation). Re-run `--pass tokens` afterwards or the join surface will
  describe the Stage-3 meanings only.

USAGE (from backend/):
  python3 scripts/populate_established_names.py --report
  python3 scripts/populate_established_names.py --lang is --dry-run
  python3 scripts/populate_established_names.py --lang is
  python3 scripts/populate_established_names.py            # all languages
  python3 scripts/populate_established_names.py --pass tokens
"""
from __future__ import annotations

import argparse
import os
import sys
from collections import Counter

sys.path.insert(0, os.getcwd())

from sqlalchemy import select, text                            # noqa: E402
from sqlalchemy.orm import Session, selectinload                # noqa: E402

from app.db.session import SessionLocal                         # noqa: E402
from app.models.generated_name import Language                  # noqa: E402
from app.models.semantic import Lexeme, Sense                   # noqa: E402
from app.services.established_names import (                    # noqa: E402
    MEANING_CHANNEL_RANK,
    classify_sense,
    extract_equivalence,
    extract_meaning,
    meaning_tokens,
    reduce_gender,
)
from app.services.romanization import (                         # noqa: E402
    extract_kaikki_romanization,
    needs_romanization,
)

BATCH = 2000

BUCKET_TO_TYPE = {
    "GIVEN": "given",
    "SURNAME": "surname",
    "PATRONYMIC": "patronymic",
}


class NameGroup:
    """Accumulator for one (normalized_lemma, name_type) key."""

    __slots__ = ("lemma", "genders", "also_surname", "best_rank",
                 "best_sense_id", "best_lexeme_id", "meaning_text",
                 "meaning_channel", "equiv_en_target", "equiv_sense_id",
                 "romanization", "sense_count")

    def __init__(self) -> None:
        self.lemma = ""
        self.genders: set[str] = set()
        self.also_surname = False
        self.best_rank = 99
        self.best_sense_id = 0
        self.best_lexeme_id = 0
        self.meaning_text: str | None = None
        self.meaning_channel: str | None = None
        self.equiv_en_target: str | None = None
        self.equiv_sense_id = 0
        self.romanization: str | None = None
        self.sense_count = 0


def collect_language(db: Session, lang, rederive_romanization: bool):
    """All shipping name senses of one language -> {(norm, type): NameGroup}."""
    groups: dict[tuple[str, str], NameGroup] = {}
    stats: Counter = Counter()

    stmt = (
        select(Sense)
        .join(Lexeme, Lexeme.id == Sense.lexeme_id)
        .options(selectinload(Sense.lexeme))
        .where(
            Lexeme.language_id == lang.id,
            Lexeme.part_of_speech == "name",
        )
        .order_by(Sense.id)
    )

    for sense in db.scalars(stmt).yield_per(BATCH):
        stats["senses_seen"] += 1
        lex = sense.lexeme
        gloss = (sense.definition or "").strip()

        bucket, gender, also_surname = classify_sense(
            gloss, list(sense.raw_tags or []), sense.categories, lang.name
        )
        if bucket not in BUCKET_TO_TYPE:
            stats[f"skipped_{bucket}"] += 1
            continue
        stats[f"kept_{bucket}"] += 1

        name_type = BUCKET_TO_TYPE[bucket]
        key = (lex.normalized_lemma, name_type)
        group = groups.get(key)
        if group is None:
            group = groups[key] = NameGroup()

        group.sense_count += 1
        group.genders.add(gender)
        group.also_surname = group.also_surname or also_surname

        meaning, channel = extract_meaning(
            gloss, sense.etymology_text or "", lang.code
        )
        rank = MEANING_CHANNEL_RANK.get(channel, 9)
        # Strictly-better wins; ties go to the LOWEST sense id, so a rerun
        # over unchanged data produces byte-identical rows.
        if rank < group.best_rank or (
            rank == group.best_rank
            and (group.best_sense_id == 0 or sense.id < group.best_sense_id)
        ):
            group.best_rank = rank
            group.best_sense_id = sense.id
            group.best_lexeme_id = lex.id
            group.meaning_text = meaning
            group.meaning_channel = channel
            group.lemma = lex.lemma

        equiv = extract_equivalence(gloss)
        if equiv and (group.equiv_sense_id == 0
                      or sense.id < group.equiv_sense_id):
            group.equiv_en_target = equiv
            group.equiv_sense_id = sense.id

        if group.romanization is None:
            value = lex.romanization
            if not value and rederive_romanization and needs_romanization(
                lang.script
            ):
                value = extract_kaikki_romanization(
                    lex.raw_entry, lang.script
                )
                if value:
                    stats["romanization_rederived"] += 1
            if value and value != lex.lemma:
                group.romanization = value

    return groups, stats


def write_names(db: Session, lang, groups, dry_run: bool) -> int:
    if dry_run:
        return len(groups)

    db.execute(
        text("DELETE FROM established_names WHERE language_id = :lid"),
        {"lid": lang.id},
    )

    rows = []
    for (normalized, name_type), g in sorted(groups.items()):
        rows.append({
            "language_id": lang.id,
            "lemma": g.lemma,
            "normalized_lemma": normalized,
            "romanization": g.romanization,
            "name_type": name_type,
            "gender": reduce_gender(g.genders),
            "is_also_surname": g.also_surname,
            "source_lexeme_id": g.best_lexeme_id,
            "source_sense_id": g.best_sense_id,
            "meaning_text": g.meaning_text,
            "meaning_channel": g.meaning_channel,
            "equiv_en_target": g.equiv_en_target,
        })

    written = 0
    insert = text("""
        INSERT INTO established_names (
            language_id, lemma, normalized_lemma, romanization, name_type,
            gender, is_also_surname, source_lexeme_id, source_sense_id,
            meaning_text, meaning_channel, equiv_en_target
        ) VALUES (
            :language_id, :lemma, :normalized_lemma, :romanization,
            :name_type, :gender, :is_also_surname, :source_lexeme_id,
            :source_sense_id, :meaning_text, :meaning_channel,
            :equiv_en_target
        )
    """)
    for start in range(0, len(rows), BATCH):
        chunk = rows[start:start + BATCH]
        db.execute(insert, chunk)
        written += len(chunk)
    db.commit()
    return written


def link_homographs(db: Session, lang, dry_run: bool) -> dict[str, int]:
    """
    Stage 3c. A name's homograph is a VISIBLE non-name lexeme of the same
    language sharing its normalized_lemma. Lowest lexeme id wins where
    several qualify -- an arbitrary but STABLE choice, so a re-run does not
    churn the column. Stage 10d's precision sample is what decides whether
    a smarter rule is needed; inventing one now would outrun the evidence.
    """
    sql = """
        UPDATE established_names en
        SET homograph_lexeme_id = m.lex_id
        FROM (
            SELECT lx.normalized_lemma AS norm, min(lx.id) AS lex_id
            FROM lexemes lx
            WHERE lx.language_id = :lid
              AND lx.part_of_speech <> 'name'
              AND EXISTS (
                  SELECT 1 FROM senses s
                  WHERE s.lexeme_id = lx.id
                    AND s.visibility_status = 'visible'
              )
            GROUP BY lx.normalized_lemma
        ) m
        WHERE en.language_id = :lid
          AND en.normalized_lemma = m.norm
    """
    if dry_run:
        count = db.execute(text("""
            SELECT count(*) FROM established_names en
            WHERE en.language_id = :lid AND EXISTS (
                SELECT 1 FROM lexemes lx
                WHERE lx.language_id = :lid
                  AND lx.part_of_speech <> 'name'
                  AND lx.normalized_lemma = en.normalized_lemma
                  AND EXISTS (SELECT 1 FROM senses s
                              WHERE s.lexeme_id = lx.id
                                AND s.visibility_status = 'visible')
            )
        """), {"lid": lang.id}).scalar_one()
        return {"linked": int(count)}

    db.execute(
        text("UPDATE established_names SET homograph_lexeme_id = NULL "
             "WHERE language_id = :lid"),
        {"lid": lang.id},
    )
    result = db.execute(text(sql), {"lid": lang.id})
    db.commit()
    return {"linked": int(getattr(result, "rowcount", 0) or 0)}


def english_lexeme_map(db: Session) -> dict[str, int]:
    """normalized_lemma -> lowest visible English lexeme id."""
    lang_id = db.scalar(select(Language.id).where(Language.code == "en"))
    if lang_id is None:
        return {}
    rows = db.execute(text("""
        SELECT lx.normalized_lemma AS norm, min(lx.id) AS lex_id
        FROM lexemes lx
        WHERE lx.language_id = :lid
          AND EXISTS (SELECT 1 FROM senses s
                      WHERE s.lexeme_id = lx.id
                        AND s.visibility_status = 'visible')
        GROUP BY lx.normalized_lemma
    """), {"lid": lang_id}).mappings().all()
    return {r["norm"]: r["lex_id"] for r in rows}


def build_tokens(db: Session, lang, en_map: dict[str, int],
                 dry_run: bool) -> dict[str, int]:
    stats: Counter = Counter()
    rows = db.execute(text("""
        SELECT id, meaning_text FROM established_names
        WHERE language_id = :lid AND meaning_text IS NOT NULL
        ORDER BY id
    """), {"lid": lang.id}).mappings().all()

    payload = []
    for row in rows:
        tokens = meaning_tokens(row["meaning_text"])
        if not tokens:
            stats["names_with_no_tokens"] += 1
            continue
        stats["names_tokenized"] += 1
        for token in tokens:
            lex_id = en_map.get(token)
            if lex_id is not None:
                stats["tokens_resolvable"] += 1
            payload.append({
                "established_name_id": row["id"],
                "token": token,
                "token_lexeme_id": lex_id,
            })
    stats["tokens_total"] = len(payload)

    if dry_run:
        return dict(stats)

    db.execute(text("""
        DELETE FROM established_name_tokens
        WHERE established_name_id IN (
            SELECT id FROM established_names WHERE language_id = :lid
        )
    """), {"lid": lang.id})
    insert = text("""
        INSERT INTO established_name_tokens
            (established_name_id, token, token_lexeme_id)
        VALUES (:established_name_id, :token, :token_lexeme_id)
    """)
    for start in range(0, len(payload), BATCH):
        db.execute(insert, payload[start:start + BATCH])
    db.commit()
    return dict(stats)


def report(db: Session) -> None:
    rows = db.execute(text("""
        SELECT l.code, en.name_type,
               count(*) AS rows,
               count(en.meaning_text) AS with_meaning,
               count(en.equiv_en_target) AS with_equiv,
               count(en.homograph_lexeme_id) AS with_homograph,
               count(en.romanization) AS with_roman,
               count(*) FILTER (WHERE en.gender = 'm') AS m,
               count(*) FILTER (WHERE en.gender = 'f') AS f,
               count(*) FILTER (WHERE en.gender = 'x') AS x,
               count(*) FILTER (WHERE en.gender = 'u') AS u,
               count(*) FILTER (WHERE en.is_also_surname) AS also_surname
        FROM established_names en
        JOIN languages l ON l.id = en.language_id
        GROUP BY l.code, en.name_type
        ORDER BY l.code, en.name_type
    """)).mappings().all()

    print(f"{'lang':5s} {'type':11s} {'rows':>7s} {'mean':>7s} {'mean%':>6s} "
          f"{'equiv':>6s} {'homo':>7s} {'roman':>7s} "
          f"{'m':>6s} {'f':>6s} {'x':>5s} {'u':>6s} {'also_sn':>7s}")
    for r in rows:
        n = max(r["rows"], 1)
        print(f"{r['code']:5s} {r['name_type']:11s} {r['rows']:7d} "
              f"{r['with_meaning']:7d} {100*r['with_meaning']/n:5.1f}% "
              f"{r['with_equiv']:6d} {r['with_homograph']:7d} "
              f"{r['with_roman']:7d} {r['m']:6d} {r['f']:6d} {r['x']:5d} "
              f"{r['u']:6d} {r['also_surname']:7d}")

    chan = db.execute(text("""
        SELECT coalesce(meaning_channel, '<none>') AS ch, count(*) AS n
        FROM established_names GROUP BY 1 ORDER BY 2 DESC
    """)).mappings().all()
    print("\n--- meaning_channel distribution (all languages) ---")
    for r in chan:
        print(f"  {r['ch']:<18} {r['n']:>8}")

    tok = db.execute(text("""
        SELECT count(*) AS total,
               count(token_lexeme_id) AS resolvable,
               count(DISTINCT token) AS distinct_tokens
        FROM established_name_tokens
    """)).mappings().one()
    total = max(tok["total"], 1)
    print(f"\n--- tokens --- total {tok['total']}, resolvable "
          f"{tok['resolvable']} ({100*tok['resolvable']/total:.1f}%), "
          f"distinct {tok['distinct_tokens']}")

    flood = db.execute(text("""
        SELECT token, count(*) AS n FROM established_name_tokens
        GROUP BY token ORDER BY n DESC LIMIT 15
    """)).mappings().all()
    print("--- top tokens (flood check) ---")
    for r in flood:
        print(f"  {r['n']:>7}  {r['token']!r}")


def target_languages(db: Session, codes: list[str] | None):
    rows = db.execute(
        select(Language.id, Language.code, Language.name, Language.script)
        .where(Language.code.isnot(None))
        .order_by(Language.code)
    ).all()
    if codes:
        wanted = set(codes)
        rows = [r for r in rows if r.code in wanted]
    return rows


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--lang", default=None, help="comma-separated ISO codes")
    ap.add_argument("--pass", dest="which", default="all",
                    choices=["all", "names", "homograph", "tokens"])
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--report", action="store_true",
                    help="print coverage only, write nothing")
    ap.add_argument("--rederive-romanization", action="store_true",
                    help="fall back to raw_entry where Lexeme.romanization "
                         "is NULL (Phase D left gaps on some name entries)")
    args = ap.parse_args()

    codes = args.lang.split(",") if args.lang else None

    with SessionLocal() as db:
        db.execute(text("SET lock_timeout = '30s'"))

        if args.report:
            report(db)
            return

        en_map: dict[str, int] = {}
        if args.which in ("all", "tokens"):
            en_map = english_lexeme_map(db)
            print(f"english lexeme map: {len(en_map)} keys")

        totals: Counter = Counter()
        for lang in target_languages(db, codes):
            print(f"\n=== {lang.code} ({lang.name}) ===")

            if args.which in ("all", "names"):
                groups, stats = collect_language(
                    db, lang, args.rederive_romanization
                )
                written = write_names(db, lang, groups, args.dry_run)
                print(f"  senses seen ......... {stats['senses_seen']}")
                for bucket in ("GIVEN", "SURNAME", "PATRONYMIC"):
                    print(f"  kept {bucket:<12} {stats[f'kept_{bucket}']}")
                print(f"  rows {'(dry-run)' if args.dry_run else 'written'} "
                      f".... {written}")
                totals["rows"] += written

            if args.which in ("all", "homograph"):
                h = link_homographs(db, lang, args.dry_run)
                print(f"  homograph linked .... {h['linked']}")
                totals["homograph"] += h["linked"]

            if args.which in ("all", "tokens"):
                t = build_tokens(db, lang, en_map, args.dry_run)
                print(f"  tokenized names ..... "
                      f"{t.get('names_tokenized', 0)}  "
                      f"(no tokens: {t.get('names_with_no_tokens', 0)})")
                print(f"  tokens .............. {t.get('tokens_total', 0)}  "
                      f"(resolvable: {t.get('tokens_resolvable', 0)})")
                totals["tokens"] += t.get("tokens_total", 0)

        print(f"\nTOTAL rows={totals['rows']}  "
              f"homograph={totals['homograph']}  tokens={totals['tokens']}")


if __name__ == "__main__":
    main()