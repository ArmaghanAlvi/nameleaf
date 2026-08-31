"""
Stage 19a -- build `name_origin_twins`, the cross-language romanization
lookup that feeds the origin prompt as evidence.

THE QUESTION. For each English name row with no category origin: does a
same-name_type name exist in ANOTHER language whose
COALESCE(romanization, lemma) normalizes to the same key? `Amal` has an
Arabic twin; `Abigail` has none. That is close to a mechanical
implementation of the adaptation criterion the given-name prompt asks about.

WHY IT EXISTS DESPITE G-3. The twin probe measured 7.8%, below the
pre-stated 10% corroboration gate, so it is NOT a gate. It succeeds as three
other things: prompt evidence, a stratification axis for Stage 20's
precision sample, and a stored provenance value (`llm_twin`) that lets a
strict bar be adopted later as a display-time change over data already held
-- no re-run, no additional quota.

KEYED IN PYTHON, NOT SQL. normalize_lemma's policy is language-tiered, and
the twin key is the ROMANIZATION folded under the ENGLISH policy, not the
twin language's own stored normalized_lemma. A SQL lower() would be the
wrong policy AND would defeat every index.

FULL REBUILD, NEVER PARTIAL, and GLOBAL not per-language: an English row's
twins live in other languages, so a per-language build is incoherent by
construction. Same reasoning as build_name_graph.py.

RUN ORDER. After any `--pass names`, and BEFORE
`populate_established_names.py --pass origin`:
    names -> homograph -> build_name_graph -> propagate_name_meanings
    -> tokens -> build_name_origin_twins -> origin

READ-ONLY except for its own table.

USAGE (from backend/):
  python3 scripts/build_name_origin_twins.py --dry-run
  python3 scripts/build_name_origin_twins.py
  python3 scripts/build_name_origin_twins.py --report
"""
from __future__ import annotations

import argparse, os, sys
from collections import Counter, defaultdict

sys.path.insert(0, os.getcwd())

from sqlalchemy import text                                       # noqa: E402
from sqlalchemy.orm import Session                                # noqa: E402

from app.db.session import SessionLocal                           # noqa: E402
from app.utils.text import normalize_lemma                        # noqa: E402

BATCH = 2000
HOST_CODE = "en"

_HOST_SQL = text("""
SELECT en.normalized_lemma, en.name_type, en.lemma,
       en.homograph_lexeme_id IS NOT NULL AS is_exempt
FROM established_names en
JOIN languages l ON l.id = en.language_id
WHERE l.code = :code
  AND en.origin_language_name IS NULL
""")

_TWIN_SQL = text("""
SELECT en.language_id, l.code, en.name_type, en.lemma, en.romanization
FROM established_names en
JOIN languages l ON l.id = en.language_id
WHERE l.code <> :code
""")


def build(db: Session, dry_run: bool) -> dict[str, int]:
    host_lang_id = db.scalar(
        text("SELECT id FROM languages WHERE code = :c"), {"c": HOST_CODE})
    stats: Counter = Counter()

    # Bucket every non-English name by (english-folded key, name_type).
    buckets: dict[tuple[str, str], list[tuple[int, str, str]]] = \
        defaultdict(list)
    for lang_id, _code, name_type, lemma, roman in db.execute(
            _TWIN_SQL, {"code": HOST_CODE}):
        surface = roman or lemma
        if not surface:
            continue
        key = normalize_lemma(surface, HOST_CODE)
        if not key:
            continue
        buckets[(key, name_type)].append((lang_id, lemma, key))
        stats["twin_side_rows"] += 1
    stats["twin_side_keys"] = len(buckets)

    rows: list[dict] = []
    hosts_with_twin: set[tuple[str, str]] = set()
    for norm, name_type, _lemma, is_exempt in db.execute(
            _HOST_SQL, {"code": HOST_CODE}):
        stats["host_rows"] += 1
        # normalized_lemma on an English row is ALREADY normalize_lemma(
        # lemma, 'en') -- the stored key and the fresh key are the same
        # function, so re-deriving it here would only risk drift.
        candidates = buckets.get((norm, name_type))
        if not candidates:
            continue
        hosts_with_twin.add((norm, name_type))
        stats["host_rows_with_twin"] += 1
        if is_exempt:
            stats["host_rows_with_twin_but_exempt"] += 1
        seen_langs: set[int] = set()
        for twin_lang_id, twin_lemma, key in candidates:
            if twin_lang_id in seen_langs:   # one row per twin LANGUAGE
                continue
            seen_langs.add(twin_lang_id)
            rows.append({
                "language_id": host_lang_id,
                "normalized_lemma": norm,
                "name_type": name_type,
                "twin_language_id": twin_lang_id,
                "twin_lemma": twin_lemma,
                "match_key": key,
            })
    stats["twin_rows"] = len(rows)

    if dry_run:
        return dict(stats)

    db.execute(text("DELETE FROM name_origin_twins WHERE language_id = :lid"),
               {"lid": host_lang_id})
    insert = text("""
        INSERT INTO name_origin_twins
            (language_id, normalized_lemma, name_type,
             twin_language_id, twin_lemma, match_key)
        VALUES (:language_id, :normalized_lemma, :name_type,
                :twin_language_id, :twin_lemma, :match_key)
    """)
    for start in range(0, len(rows), BATCH):
        db.execute(insert, rows[start:start + BATCH])
    db.commit()
    return dict(stats)


def report(db: Session) -> None:
    n = db.execute(text("""
        SELECT count(*) AS twin_rows,
               count(DISTINCT (normalized_lemma, name_type)) AS host_rows
        FROM name_origin_twins
    """)).mappings().one()
    print(f"twin rows {n['twin_rows']}, distinct host rows {n['host_rows']}")

    print("\n--- twin languages by frequency ---")
    for r in db.execute(text("""
        SELECT l.code, count(*) AS n
        FROM name_origin_twins t JOIN languages l ON l.id = t.twin_language_id
        GROUP BY l.code ORDER BY n DESC
    """)).mappings():
        print(f"  {r['code']:<6} {r['n']:>7}")

    print("\n--- by host name_type ---")
    for r in db.execute(text("""
        SELECT name_type, count(*) AS twin_rows,
               count(DISTINCT normalized_lemma) AS hosts
        FROM name_origin_twins GROUP BY name_type ORDER BY 2 DESC
    """)).mappings():
        print(f"  {r['name_type']:<12} rows={r['twin_rows']:>7} "
              f"hosts={r['hosts']:>7}")

    print("\n--- 15 sample twins ---")
    for r in db.execute(text("""
        SELECT t.normalized_lemma, t.name_type, l.code, t.twin_lemma
        FROM name_origin_twins t JOIN languages l ON l.id = t.twin_language_id
        ORDER BY t.normalized_lemma LIMIT 15
    """)).mappings():
        print(f"  {r['normalized_lemma']:<20} {r['name_type']:<10} "
              f"{r['code']:<5} {r['twin_lemma']}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--report", action="store_true")
    args = ap.parse_args()

    with SessionLocal() as db:
        db.execute(text("SET lock_timeout = '30s'"))
        if args.report:
            report(db)
            return
        stats = build(db, args.dry_run)
        for k in sorted(stats):
            print(f"  {k:<34} {stats[k]}")


if __name__ == "__main__":
    main()