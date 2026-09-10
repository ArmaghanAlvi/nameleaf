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
  origin     re-apply display_origin_language / origin_source from the
             derived sources and name_origin_attempts (Stage 18d)
  all        names -> homograph -> tokens -> origin

  ⚠ `origin` MUST follow `homograph`: the gradient exemption reads
  homograph_lexeme_id. It reads no meaning and feeds no edge, so
  build_name_graph.py and propagate_name_meanings.py do NOT need re-running
  after it. The reverse is not true -- propagate_name_meanings rewrites
  meaning_text, which is INPUT to the Stage-22 origin prompt, so a full
  rebuild that will be followed by asking runs:
      names -> homograph -> build_name_graph -> propagate_name_meanings
      -> tokens -> build_name_origin_twins -> origin

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
from typing import TypedDict, cast

sys.path.insert(0, os.getcwd())

from sqlalchemy import bindparam, select, text                  # noqa: E402
from sqlalchemy.orm import Session, selectinload                # noqa: E402
from sqlalchemy.engine import CursorResult

from app.db.session import SessionLocal                         # noqa: E402
from app.models.generated_name import Language                  # noqa: E402
from app.models.semantic import Lexeme, Sense                   # noqa: E402
from app.services.established_names import (                    # noqa: E402
    MEANING_CHANNEL_RANK,
    TOKENIZED_CHANNELS,
    classify_sense_origin,
    language_header_warning,
    extract_equivalence,
    extract_meaning,
    meaning_tokens,
    reduce_gender,
)
from app.services.name_origin_llm import ANCESTRAL_ENGLISH     # noqa: E402
from app.services.romanization import (                        # noqa: E402
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
                 "romanization", "sense_count", "origin_language",
                 "origin_shape", "header_warning")

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
        self.origin_language: str | None = None
        self.origin_shape: str | None = None
        self.header_warning = False


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

        bucket, gender, also_surname, origin, origin_shape = (
            classify_sense_origin(
                gloss, list(sense.raw_tags or []), sense.categories,
                lang.name
            )
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

        # Reduced across the group, not taken from the winning sense: the
        # meaning waterfall picks source_sense_id on MEANING evidence, and
        # the origin routinely sits on a sibling sense that lost it. Same
        # reason build_name_graph reads edges from senses rather than from
        # source_sense_id. Precedence matches
        # classify_from_categories_origin: 'rendering' beats 'from', and
        # within a shape the first sense seen wins (senses are ordered by
        # id, so a rerun is byte-identical).
        if origin and (
            group.origin_language is None
            or (origin_shape == "rendering"
                and group.origin_shape == "from")
        ):
            group.origin_language = origin
            group.origin_shape = origin_shape
        group.header_warning = group.header_warning or language_header_warning(
            sense.categories, lang.name
        )

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
            "origin_language_name": g.origin_language,
            "origin_shape": g.origin_shape,
            "language_header_warning": g.header_warning,
        })

    written = 0
    insert = text("""
        INSERT INTO established_names (
            language_id, lemma, normalized_lemma, romanization, name_type,
            gender, is_also_surname, source_lexeme_id, source_sense_id,
            meaning_text, meaning_channel, equiv_en_target,
            origin_language_name, origin_shape, language_header_warning
        ) VALUES (
            :language_id, :lemma, :normalized_lemma, :romanization,
            :name_type, :gender, :is_also_surname, :source_lexeme_id,
            :source_sense_id, :meaning_text, :meaning_channel,
            :equiv_en_target, :origin_language_name, :origin_shape,
            :language_header_warning
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


class OriginApplyResult(TypedDict):
    """apply_origin's return shape. `final` is the one field that isn't a
    plain int -- it's the per-origin_source breakdown of the table's
    ACTUAL final state, printed alongside the tier writes because the two
    numbers legitimately differ (54-row exempt/category overlap; see the
    comment on `final`'s assignment below). A TypedDict keeps that one
    field's shape distinct from the rest instead of blending everything
    into one int | dict union, which is what broke every arithmetic and
    .items() call on the other fields."""
    exempt: int
    category: int
    gloss_etym: int
    ledger: int
    folded: int
    final: dict[str, int]
    overlap: int


def apply_origin(db: Session, lang, dry_run: bool) -> OriginApplyResult:
    """
    Stage 18d. Re-apply origins onto established_names from the DERIVED and
    LEDGER sources, in precedence order.

    MUST RUN AFTER --pass homograph: step 1 reads homograph_lexeme_id, which
    that pass sets. The `all` cascade orders them correctly; a manual
    `--pass origin` on a freshly rebuilt language without homographs would
    silently produce zero exemptions.

    PRECEDENCE, and why each:
      1. gradient_exempt  a homograph row is by construction a name spelled
                          identically to a word of the SAME language, so
                          labelling it native is correct whether or not it
                          renders gradient. Written as an explicit VALUE
                          rather than left NULL, because NULL means
                          "pending" and exempt rows would otherwise be
                          re-queued forever.
      2. category         a parsed Wiktionary category is stronger evidence
                          than a model assertion, so it OVERWRITES (1).
      3. gloss_etym       a single unambiguous "from <Language>" phrase in
                          the WINNING sense's own gloss or the first
                          sentence of its etymology. Fills gaps only --
                          weaker evidence than a structured category, so it
                          never overwrites (2). A row where the phrase
                          names MORE than one language is left NULL here
                          on purpose: that is exactly the ambiguity the
                          three-pass LLM mechanism exists to arbitrate, and
                          picking first-match or last-match would require
                          believing something about Wiktionary's phrasing
                          convention that was never verified (§23.1).
      4. ledger           fills only what is STILL NULL, so the model can
                          never override a derived or extracted origin.

    The exemption is DERIVED here rather than stored in the ledger. It is
    computable from a column on the same row, so a ledger entry would buy
    nothing and would go stale the first time a rebuild changed a row's
    homograph status. The ledger exists to protect data that was PAID FOR.
    """
    if dry_run:
        counts = db.execute(text("""
            SELECT count(*) FILTER (WHERE homograph_lexeme_id IS NOT NULL)
                       AS exempt,
                   count(*) FILTER (WHERE origin_language_name IS NOT NULL)
                       AS category
            FROM established_names WHERE language_id = :lid
        """), {"lid": lang.id}).mappings().one()
        return OriginApplyResult(
            exempt=counts["exempt"], category=counts["category"],
            gloss_etym=0, ledger=0, folded=0, final={}, overlap=0,
        )

    db.execute(text("""
        UPDATE established_names
        SET display_origin_language = NULL, origin_source = NULL
        WHERE language_id = :lid
    """), {"lid": lang.id})

    exempt = cast(CursorResult, db.execute(text("""
        UPDATE established_names
        SET origin_source = 'gradient_exempt'
        WHERE language_id = :lid AND homograph_lexeme_id IS NOT NULL
    """), {"lid": lang.id})).rowcount or 0

    category = cast(CursorResult, db.execute(text("""
        UPDATE established_names
        SET origin_source = 'category',
            display_origin_language = origin_language_name
        WHERE language_id = :lid AND origin_language_name IS NOT NULL
    """), {"lid": lang.id})).rowcount or 0

    gloss_etym = cast(CursorResult, db.execute(text("""
        UPDATE established_names en
        SET origin_source = 'gloss_etym', display_origin_language = m.lang_name
        FROM (
            SELECT p.id, min(lang.name) AS lang_name
            FROM (
                SELECT en2.id, s.definition AS gloss,
                       split_part(s.etymology_text, '.', 1) AS etym1
                FROM established_names en2
                LEFT JOIN senses s ON s.id = en2.source_sense_id
                WHERE en2.language_id = :lid AND en2.origin_source IS NULL
            ) p
            JOIN languages lang ON lang.code IS NOT NULL
            WHERE p.gloss ~* ('\\yfrom ' || lang.name || '\\y')
               OR p.etym1 ~* ('\\yfrom ' || lang.name || '\\y')
            GROUP BY p.id
            -- SINGLE match only. More than one language named is the
            -- ambiguity the LLM pass exists for -- see the precedence
            -- docstring above.
            HAVING count(DISTINCT lang.name) = 1
        ) m
        WHERE en.id = m.id
    """), {"lid": lang.id})).rowcount or 0

    ledger = cast(CursorResult, db.execute(text("""
        UPDATE established_names en
        SET origin_source = a.src, display_origin_language = a.disp
        FROM (
            SELECT n.language_id, n.normalized_lemma, n.name_type,
                   CASE n.status
                     WHEN 'error'     THEN 'llm_error'
                     WHEN 'disagreed' THEN 'llm_error'
                     WHEN 'unknown'   THEN 'llm_unknown'
                     ELSE CASE
                       WHEN n.origin = l.name THEN 'llm_native'
                       -- CORROBORATION, not exposure. twin_language is
                       -- written only when the twin MATCHES the resolved
                       -- origin, and this predicate enforces the same rule
                       -- at the read site so `llm_twin` cannot come to
                       -- mean "we showed the model a twin" again. Every
                       -- twin that was SENT is still recoverable from
                       -- name_origin_twins.
                       WHEN n.twin_language IS NOT NULL
                        AND n.twin_language = n.origin THEN 'llm_twin'
                       ELSE 'llm_foreign' END
                   END AS src,
                   -- D-4. `origin` is the BACKEND MARKER ('other');
                   -- `origin_raw` is the badge string ('Turkish'). Writing
                   -- n.origin here would put the literal word "other" on
                   -- the card.
                   CASE WHEN n.status = 'resolved'
                        THEN n.origin_raw END AS disp
            FROM name_origin_attempts n
            JOIN languages l ON l.id = n.language_id
            WHERE n.language_id = :lid
        ) a
        WHERE en.language_id = a.language_id
          AND en.normalized_lemma = a.normalized_lemma
          AND en.name_type = a.name_type
          AND en.origin_source IS NULL
    """), {"lid": lang.id})).rowcount or 0

    # THE FOLD, AT THE WRITE SITE. (23.10, closed in Breakdown K.)
    #
    # Until now the ancestral-English fold lived in exactly one place --
    # name_origin_llm.normalize_origin -- reachable ONLY through the LEDGER
    # tier, because that tier reads origin_raw, which was normalised when
    # it was written. The `category` and `gloss_etym` UPDATEs above each
    # take a language NAME straight out of Wiktionary text and write it to
    # display_origin_language without ever passing through it. gloss_etym
    # alone is an order of magnitude larger than the LLM-resolved
    # population, which is why 1,925 production rows read "Old English"
    # after Stage 22 despite D-5, and why the one-time UPDATE could not be
    # the fix: --pass origin resets the language and re-derives, so the
    # leak comes back on the next rebuild.
    #
    # RUNS LAST, so it covers every tier including any future one. Gated on
    # an English host, exactly as normalize_origin is: "Old English" is a
    # truthful, in-vocabulary answer for a RUSSIAN row and folding it there
    # would be a defect, not a fix. Old Norse and Frankish stay out for the
    # reason given at ANCESTRAL_ENGLISH's definition.
    folded = 0
    if lang.code == "en":
        folded = cast(CursorResult, db.execute(text("""
            UPDATE established_names
            SET display_origin_language = :host
            WHERE language_id = :lid
              AND display_origin_language IS NOT NULL
              AND lower(display_origin_language) IN :ancestral
        """).bindparams(bindparam("ancestral", expanding=True)),
            {"lid": lang.id, "host": lang.name,
             "ancestral": sorted(ANCESTRAL_ENGLISH)})).rowcount or 0

    # WRITES vs. FINAL STATE. The rowcounts above are what each UPDATE
    # touched; `category` deliberately overwrites gradient rows, so 54 rows
    # are counted by both `exempt` and `category` while holding exactly one
    # final value. That discrepancy between the printed TOTAL and the
    # summed per-source table has been re-derived from scratch in 23.5 and
    # again in 23.11. Printing the final state next to the writes retires
    # the question instead of documenting it a fourth time.
    final = {r.origin_source or "(pending)": r.rows for r in db.execute(text("""
        SELECT origin_source, count(*) AS rows
        FROM established_names WHERE language_id = :lid
        GROUP BY 1 ORDER BY 2 DESC
    """), {"lid": lang.id})}

    db.commit()
    return {"exempt": exempt, "category": category,
            "gloss_etym": gloss_etym, "ledger": ledger,
            "folded": folded, "final": final, "overlap": 0}


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
    # Not every meaning belongs on the mechanism-1 join surface. HOMOGRAPH
    # glosses are real dictionary prose at spelling_only precision and would
    # multiply this table several-fold; TOKENIZED_CHANNELS is where that
    # policy lives, set by the Step-7 measurement.
    rows = db.execute(
        text("SELECT id, meaning_text FROM established_names "
             "WHERE language_id = :lid AND meaning_text IS NOT NULL "
             "AND meaning_channel IN :chans ORDER BY id").bindparams(
            bindparam("chans", expanding=True)
        ),
        {"lid": lang.id, "chans": sorted(TOKENIZED_CHANNELS)},
    ).mappings().all()

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
               count(*) FILTER (WHERE en.is_also_surname) AS also_surname,
               count(en.origin_language_name) AS with_origin,
               count(*) FILTER (WHERE en.language_header_warning) AS hdr_warn,
               count(en.display_origin_language) AS disp_origin,
               count(*) FILTER (WHERE en.origin_source IS NOT NULL)
                   AS origin_src
        FROM established_names en
        JOIN languages l ON l.id = en.language_id
        GROUP BY l.code, en.name_type
        ORDER BY l.code, en.name_type
    """)).mappings().all()

    print(f"{'lang':5s} {'type':11s} {'rows':>7s} {'mean':>7s} {'mean%':>6s} "
          f"{'equiv':>6s} {'homo':>7s} {'roman':>7s} "
          f"{'m':>6s} {'f':>6s} {'x':>5s} {'u':>6s} {'also_sn':>7s} "
          f"{'origin':>7s} {'hdrwarn':>7s} {'disp':>7s} {'osrc':>7s}")
    for r in rows:
        n = max(r["rows"], 1)
        print(f"{r['code']:5s} {r['name_type']:11s} {r['rows']:7d} "
              f"{r['with_meaning']:7d} {100*r['with_meaning']/n:5.1f}% "
              f"{r['with_equiv']:6d} {r['with_homograph']:7d} "
              f"{r['with_roman']:7d} {r['m']:6d} {r['f']:6d} {r['x']:5d} "
              f"{r['u']:6d} {r['also_surname']:7d} "
              f"{r['with_origin']:7d} {r['hdr_warn']:7d} "
              f"{r['disp_origin']:7d} {r['origin_src']:7d}")

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
                    choices=["all", "names", "homograph", "tokens",
                             "origin"])
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
            # LAST in the cascade, and after `homograph` specifically: the
            # exemption reads homograph_lexeme_id. Nothing downstream reads
            # origin, so its position relative to `tokens` is free.
            if args.which in ("all", "origin"):
                o = apply_origin(db, lang, args.dry_run)
                print(f"  origin exempt ....... {o['exempt']}")
                print(f"  origin category ..... {o['category']}")
                print(f"  origin gloss/etym ... {o['gloss_etym']}")
                print(f"  origin from ledger .. {o['ledger']}")
                print(f"  origin folded to en . {o['folded']}")
                print("  origin final state ..")
                for src, n in o["final"].items():
                    print(f"      {src:<18} {n}")
                totals["origin"] += (o["exempt"] + o["category"]
                                     + o["gloss_etym"] + o["ledger"])
        print(f"\nTOTAL rows={totals['rows']}  "
              f"homograph={totals['homograph']}  tokens={totals['tokens']}  "
              f"origin={totals['origin']}")


if __name__ == "__main__":
    main()