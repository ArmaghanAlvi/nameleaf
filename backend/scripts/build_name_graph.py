"""
Stage 5 -- build `established_name_edges` and `established_name_clusters`
from the name senses already in Postgres.

RUN ORDER. This script sits between the Stage-3 populator and the Stage-6
propagator, and the order is not optional:

  1. scripts/populate_established_names.py            (Stage 3)
  2. scripts/build_name_graph.py                      (this, Stage 5)
  3. scripts/propagate_name_meanings.py               (Stage 6)
  4. scripts/populate_established_names.py --pass tokens

Step 2 must follow any `--lang X` rebuild in step 1: established_name_edges
FKs into established_names with ON DELETE CASCADE, so re-deriving one
language's rows silently deletes every edge touching it and leaves the
clusters that contained them stale.

FULL REBUILD, NEVER PARTIAL. Cross-language EQUIV_EN edges make a
per-language build incoherent (`Иван`'s target lives in `en`), and the whole
graph is only a few thousand edges, so the script always deletes everything
and re-derives. That also makes it exactly reproducible.

WHY EDGES ARE READ FROM SENSES, NOT FROM `source_sense_id`. Stage 3 collapsed
several senses of a lemma into one row and pointed `source_sense_id` at the
one that won the MEANING waterfall. That is routinely not the sense carrying
the variant trigger -- 'Asia' wins its row on a gloss with no trigger while a
sibling sense reads "a diminutive of the female given name Joanna". Building
off `source_sense_id` would drop most of the graph without erroring.

USAGE (from backend/):
  python3 scripts/build_name_graph.py --dry-run
  python3 scripts/build_name_graph.py
  python3 scripts/build_name_graph.py --report
"""
from __future__ import annotations

import argparse
import os
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.getcwd())

from sqlalchemy import bindparam, select, text                   # noqa: E402
from sqlalchemy.orm import Session, selectinload                 # noqa: E402

from app.db.session import SessionLocal                          # noqa: E402
from app.models.generated_name import Language                   # noqa: E402
from app.models.semantic import Lexeme, Sense                    # noqa: E402
from app.services.established_names import classify_sense        # noqa: E402
from app.services.name_variants import (                         # noqa: E402
    assert_cluster_ceiling,
    build_components,
    extract_edges,
    select_head,
)

BATCH = 2000
CLUSTER_CEILING = 100  # re-derived post-fix; see findings §12

BUCKET_TO_TYPE = {
    "GIVEN": "given", "SURNAME": "surname", "PATRONYMIC": "patronymic",
}


def load_name_index(db: Session):
    """
    (name_type, lang_code, normalized_lemma) -> established_name id,
    id -> (lang_code, name_type), and id -> its own source_lexeme_id.
    """
    rows = db.execute(text("""
        SELECT en.id, l.code, en.name_type, en.normalized_lemma,
               en.source_lexeme_id
        FROM established_names en
        JOIN languages l ON l.id = en.language_id
    """)).all()
    index: dict[tuple[str, str, str], int] = {}
    meta: dict[int, tuple[str, str]] = {}
    canonical_lexeme_of: dict[int, int] = {}
    for name_id, code, name_type, norm, source_lexeme_id in rows:
        index[(name_type, code, norm)] = name_id
        meta[name_id] = (code, name_type)
        canonical_lexeme_of[name_id] = source_lexeme_id
    return index, meta, canonical_lexeme_of


def collect_edges(db: Session, index, meta, canonical_lexeme_of):
    """
    -> (edges, stats, cross_type_samples). edges are
    (src_id, tgt_id, relation, is_cross), deduplicated on
    (src, tgt, relation), self-loops dropped.
    """
    lang_names: dict[int, str] = {
        row.id: row.name for row in db.execute(select(Language.id, Language.name))
    }
    lang_codes: dict[int, str] = {
        row.id: row.code for row in db.execute(select(Language.id, Language.code))
    }

    seen: set[tuple[int, int, str]] = set()
    edges: list[tuple[int, int, str, bool]] = []
    stats: Counter = Counter()
    cross_type_samples: list = []

    stmt = (
        select(Sense)
        .join(Lexeme, Lexeme.id == Sense.lexeme_id)
        .options(selectinload(Sense.lexeme))
        .where(Lexeme.part_of_speech == "name")
        .order_by(Sense.id)
    )
    for sense in db.scalars(stmt).yield_per(5000):
        lex = sense.lexeme
        code = lang_codes.get(lex.language_id)
        lang_name = lang_names.get(lex.language_id)
        if not code or not lang_name:
            continue
        gloss = (sense.definition or "").strip()
        bucket, _gender, _also = classify_sense(
            gloss, list(sense.raw_tags or []), sense.categories, lang_name
        )
        if bucket not in BUCKET_TO_TYPE:
            continue
        name_type = BUCKET_TO_TYPE[bucket]
        src_id = index.get((name_type, code, lex.normalized_lemma))
        if src_id is None:
            stats["source_row_missing"] += 1
            continue
        if canonical_lexeme_of[src_id] != lex.id:
            # This sense belongs to a DIFFERENT lexeme that happens to share
            # this row's (lang, normalized_lemma, name_type) key -- e.g. two
            # Wiktionary etymology sections for the same spelling ("Alan":
            # Celtic-origin vs. a Hebrew variant of Elon). Attaching this
            # sense's edges to the row would silently bridge two unrelated
            # identities through a coincidence of spelling. Blank over wrong:
            # only the row's own canonical lexeme may source its edges.
            stats["non_canonical_lexeme_skipped"] += 1
            continue

        for rel, target_lang, norm_target in extract_edges(gloss, code):
            stats["candidates"] += 1
            tgt_id = index.get((name_type, target_lang, norm_target))
            if tgt_id is None:
                # 5e: cross-TYPE candidates are counted, never unioned.
                other = "surname" if name_type == "given" else "given"
                if (other, target_lang, norm_target) in index:
                    stats["cross_type_candidates"] += 1
                    stats[f"cross_type_{rel}"] += 1
                    if len(cross_type_samples) < 20:
                        cross_type_samples.append(
                            (rel, f"{code}:{lex.lemma} ({name_type})",
                             f"{target_lang}:{norm_target} ({other})")
                        )
                else:
                    stats["unresolved"] += 1
                continue
            if tgt_id == src_id:
                stats["self_loop"] += 1
                continue
            key = (src_id, tgt_id, rel)
            if key in seen:
                stats["duplicate"] += 1
                continue
            seen.add(key)
            # is_cross_language is decided by the ACTUAL languages, not by
            # the relation label: an English source carrying EQUIV_EN points
            # at English and is a SAME-language edge despite the name.
            is_cross = meta[src_id][0] != meta[tgt_id][0]
            edges.append((src_id, tgt_id, rel, is_cross))
            stats["resolved"] += 1
            stats[f"rel_{rel}"] += 1
            stats["cross_language" if is_cross else "same_language"] += 1

    return edges, stats, cross_type_samples


def write_edges(db: Session, edges, dry_run: bool) -> int:
    if dry_run:
        return len(edges)
    db.execute(text("UPDATE established_names SET cluster_id = NULL"))
    db.execute(text("DELETE FROM established_name_clusters"))
    db.execute(text("DELETE FROM established_name_edges"))
    insert = text("""
        INSERT INTO established_name_edges
            (source_name_id, target_name_id, relation_type, is_cross_language)
        VALUES (:s, :t, :r, :x)
    """)
    payload = [{"s": s, "t": t, "r": r, "x": x} for s, t, r, x in edges]
    for start in range(0, len(payload), BATCH):
        db.execute(insert, payload[start:start + BATCH])
    db.commit()
    return len(payload)


def build_clusters(db: Session, edges, meta, dry_run: bool):
    """
    Components over SAME-LANGUAGE edges only, computed independently per
    name_type (5e: two graphs, never unioned).

    `is_cross_language_merged` is computed from the members' ACTUAL
    languages. Under the adopted containment rule it can only ever be false,
    which is the point -- the column stops being a dead feature flag and
    becomes a build-time detector for a containment leak.
    """
    stats: Counter = Counter()
    by_type: dict[str, list] = defaultdict(list)
    for src, tgt, rel, is_cross in edges:
        by_type[meta[src][1]].append((src, tgt, rel, is_cross))

    written = 0
    for name_type, type_edges in sorted(by_type.items()):
        components = build_components(type_edges)
        ok, largest = assert_cluster_ceiling(components, CLUSTER_CEILING)
        stats[f"{name_type}_components"] = len(components)
        stats[f"{name_type}_largest"] = largest
        sizes = Counter(len(v) for v in components.values())
        print(f"  [{name_type}] components={len(components)} "
              f"largest={largest} ceiling_ok={ok}")
        for size in sorted(sizes, reverse=True)[:8]:
            print(f"      size {size:>4}: {sizes[size]:>5} component(s)")
        if not ok:
            print(f"  !! CEILING BREACH ({largest} > {CLUSTER_CEILING}) -- "
                  f"containment rule did not hold; do NOT ship this graph.")

        if dry_run:
            continue
        for _rep, members in components.items():
            head = select_head(members, type_edges)
            langs = {meta[m][0] for m in members}
            merged = len(langs) > 1
            if merged:
                stats["INVARIANT_VIOLATION_cross_language_cluster"] += 1
            cluster_id = db.execute(text("""
                INSERT INTO established_name_clusters
                    (name_type, head_name_id, size, is_cross_language_merged)
                VALUES (:t, :h, :s, :m) RETURNING id
            """), {"t": name_type, "h": head, "s": len(members),
                   "m": merged}).scalar_one()
            db.execute(
                text("UPDATE established_names SET cluster_id = :c "
                     "WHERE id IN :ids").bindparams(
                    bindparam("ids", expanding=True)
                ),
                {"c": cluster_id, "ids": members},
            )
            written += 1
        db.commit()
    stats["clusters_written"] = written
    return stats


def report(db: Session) -> None:
    rows = db.execute(text("""
        SELECT relation_type, is_cross_language, count(*) AS n
        FROM established_name_edges
        GROUP BY 1, 2 ORDER BY 1, 2
    """)).mappings().all()
    print("--- edges by relation x scope ---")
    for r in rows:
        scope = "cross-language" if r["is_cross_language"] else "same-language"
        print(f"  {r['relation_type']:<16} {scope:<15} {r['n']:>7}")

    cl = db.execute(text("""
        SELECT name_type, count(*) AS clusters, max(size) AS largest,
               sum(size) AS members,
               count(*) FILTER (WHERE is_cross_language_merged) AS merged
        FROM established_name_clusters GROUP BY 1 ORDER BY 1
    """)).mappings().all()
    print("\n--- clusters ---")
    for r in cl:
        print(f"  {r['name_type']:<11} clusters={r['clusters']:>6} "
              f"members={r['members']:>7} largest={r['largest']:>4} "
              f"cross_language_merged={r['merged']}")

    big = db.execute(text("""
        SELECT c.id, c.name_type, c.size, en.lemma AS head, l.code
        FROM established_name_clusters c
        LEFT JOIN established_names en ON en.id = c.head_name_id
        LEFT JOIN languages l ON l.id = en.language_id
        ORDER BY c.size DESC LIMIT 10
    """)).mappings().all()
    print("\n--- 10 largest clusters (head shown) ---")
    for r in big:
        print(f"  size={r['size']:<5} {r['name_type']:<10} "
              f"{r['code']}:{r['head']}")

    leaves = db.execute(text("""
        SELECT count(*) FROM established_name_edges WHERE is_cross_language
    """)).scalar_one()
    orphan = db.execute(text("""
        SELECT count(*) FROM established_names WHERE cluster_id IS NULL
    """)).scalar_one()
    print(f"\ncross-language leaf edges: {leaves}")
    print(f"names in no cluster (singletons): {orphan}")


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
        index, meta, canonical_lexeme_of = load_name_index(db)
        print(f"name index: {len(index)} rows")
        edges, stats, cross_type = collect_edges(
            db, index, meta, canonical_lexeme_of
        )
        print(f"\ncandidates={stats['candidates']} "
              f"resolved={stats['resolved']} "
              f"cross_type={stats['cross_type_candidates']} "
              f"unresolved={stats['unresolved']} "
              f"self_loops={stats['self_loop']} dupes={stats['duplicate']} "
              f"non_canonical_skipped={stats['non_canonical_lexeme_skipped']}")
        for rel in ("EQUIV_EN", "VARIANT_OF", "DIMINUTIVE_OF",
                    "FEM_EQUIV", "MASC_EQUIV"):
            print(f"  {rel:<16} {stats[f'rel_{rel}']:>7}")
        print(f"  same-language {stats['same_language']:>7}   "
              f"cross-language {stats['cross_language']:>7}")
        print("\n--- 5e cross-TYPE candidates (counted, NOT unioned) ---")
        for rel, src, tgt in cross_type:
            print(f"      [{rel}] {src} -> {tgt}")

        written = write_edges(db, edges, args.dry_run)
        print(f"\nedges {'(dry-run)' if args.dry_run else 'written'}: "
              f"{written}")
        cstats = build_clusters(db, edges, meta, args.dry_run)
        if cstats.get("INVARIANT_VIOLATION_cross_language_cluster"):
            print("!! INVARIANT VIOLATION: a cluster spans >1 language.")
        print(f"clusters {'(dry-run)' if args.dry_run else 'written'}: "
              f"{cstats.get('clusters_written', 0)}")


if __name__ == "__main__":
    main()