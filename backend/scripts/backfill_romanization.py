"""
D4 -- populate Lexeme.romanization for non-Latn-script languages.

DB-SIDE ONLY. No corpus files are read: Lexeme.raw_entry holds the complete
Kaikki entry (kaikki_english.py:35), and Lexeme has exactly one construction
site, so every lexeme in the DB has one. This also means the backfill and the
D1 census share extract_kaikki_romanization() -- they cannot disagree.

TWO PASSES, SELECTABLE:
  --source kaikki       (default) Kaikki containers only
  --source algorithmic  in-repo tables (Cyrl/Grek ONLY), only where
                         romanization is still NULL
  --source both

Algorithmic NEVER overwrites a Kaikki value: the dictionary's per-word answer
beats a character table every time it exists. Per the Step 1 census, Kaikki
alone already covers 99.3-100% of every scripted language except zh, so the
algorithmic pass is expected to be a very small mop-up, if it's needed at all
-- Step 4's own coverage report is what decides that, not a guess made here.

RESUMABLE: only rows WHERE romanization IS NULL are considered, so an
interrupted run resumes by re-running. --force re-derives everything for a
language (use after changing _PREFERENCE).

USAGE (from backend/):
  python3 scripts/backfill_romanization.py --dry-run
  python3 scripts/backfill_romanization.py --lang ru
  python3 scripts/backfill_romanization.py
  python3 scripts/backfill_romanization.py --source algorithmic --lang ru,el
  python3 scripts/backfill_romanization.py --report
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.getcwd())

from sqlalchemy import select, text                          # noqa: E402
from sqlalchemy.orm import Session                           # noqa: E402

from app.db.session import SessionLocal                      # noqa: E402
from app.models.generated_name import Language                # noqa: E402
from app.services.romanization import (                      # noqa: E402
    MAX_ROMANIZATION_LEN,
    algorithmic_romanization,
    extract_kaikki_romanization,
    needs_romanization,
)

BATCH = 2000


def target_languages(db: Session, codes: list[str] | None):
    rows = db.execute(
        select(Language.id, Language.code, Language.script)
        .where(Language.code.isnot(None))
        .order_by(Language.code)
    ).all()
    rows = [r for r in rows if needs_romanization(r.script)]
    if codes:
        wanted = set(codes)
        rows = [r for r in rows if r.code in wanted]
    return rows


def backfill_language(
    db: Session,
    lang,
    source: str,
    force: bool,
    dry_run: bool,
) -> dict[str, int]:
    stats = {
        "seen": 0, "kaikki": 0, "algorithmic": 0,
        "blank": 0, "overlong_discarded": 0, "written": 0,
    }

    want_raw = source in ("kaikki", "both")

        # KEYSET pagination on id, not OFFSET. OFFSET needed three different
    # correct behaviours depending on mode -- the window shrinks as rows are
    # written (normal), never shrinks (--dry-run, --force), and NEVER fully
    # empties in normal mode either, because rows that derive no value stay
    # NULL and so stay in the window forever, re-fetched on every lap. Keyset
    # has one behaviour in all three modes: always move past the last id
    # seen. It is also faster on zh/ja, where OFFSET would scan ever-growing
    # prefixes.
    if want_raw:
        stmt = text("""
            SELECT lx.id, lx.lemma, (lx.raw_entry::jsonb) AS raw
            FROM lexemes lx
            WHERE lx.language_id = :lid
              AND lx.id > :after_id
              AND (:force OR lx.romanization IS NULL)
            ORDER BY lx.id
            LIMIT :lim
        """)
    else:
        stmt = text("""
            SELECT lx.id, lx.lemma, NULL::jsonb AS raw
            FROM lexemes lx
            WHERE lx.language_id = :lid
              AND lx.id > :after_id
              AND (:force OR lx.romanization IS NULL)
            ORDER BY lx.id
            LIMIT :lim
        """)

    after_id = 0
    while True:
        rows = db.execute(stmt, {
            "lid": lang.id, "force": force, "lim": BATCH,
            "after_id": after_id,
        }).mappings().all()

        if not rows:
            break

        # Rows are ORDER BY lx.id, so the last one is the high-water mark.
        # Advancing unconditionally is what makes this loop mode-independent.
        after_id = rows[-1]["id"]

        updates: list[dict] = []

        for row in rows:
            stats["seen"] += 1
            value = None

            if source in ("kaikki", "both"):
                value = extract_kaikki_romanization(row["raw"], lang.script)
                if value:
                    stats["kaikki"] += 1

            if not value and source in ("algorithmic", "both"):
                value = algorithmic_romanization(row["lemma"], lang.script)
                if value:
                    stats["algorithmic"] += 1

            if not value:
                stats["blank"] += 1
                continue

            if len(value) > MAX_ROMANIZATION_LEN:
                stats["overlong_discarded"] += 1
                continue

            # Never store a "romanization" identical to the lemma -- a no-op
            # that costs a UI line and tells the reader nothing.
            if value == row["lemma"]:
                stats["blank"] += 1
                continue

            updates.append({"id": row["id"], "rom": value})

        if updates and not dry_run:
            # Core text() executemany, NOT update(Lexeme). SQLAlchemy 2.x's
            # ORM-enabled bulk update refuses this shape twice over: it can't
            # synchronize a per-row bindparam WHERE, and its per-row-by-PK
            # path demands the PK under its mapped attribute name. Neither
            # constraint buys anything here -- this script reads rows via
            # raw text() SQL and never materializes Lexeme instances, so
            # there is no session state to keep in sync. Going straight to
            # Core sidesteps both.
            db.execute(
                text("UPDATE lexemes SET romanization = :rom WHERE id = :id"),
                updates,
            )
            db.commit()

        stats["written"] += len(updates)

        print(f"    {lang.code}: {stats['seen']:7d} seen  "
              f"{stats['written']:7d} written", end="\r", flush=True)

    print()
    return stats


def report(db: Session) -> None:
    rows = db.execute(text("""
        SELECT l.code, l.script,
               count(*) AS total,
               count(lx.romanization) AS filled,
               count(*) FILTER (
                 WHERE lx.romanization IS NOT NULL AND EXISTS (
                   SELECT 1 FROM senses s
                   WHERE s.lexeme_id = lx.id
                     AND s.visibility_status = 'visible')
               ) AS filled_visible,
               count(*) FILTER (
                 WHERE EXISTS (
                   SELECT 1 FROM senses s
                   WHERE s.lexeme_id = lx.id
                     AND s.visibility_status = 'visible')
               ) AS total_visible,
               max(length(lx.romanization)) AS maxlen
        FROM lexemes lx
        JOIN languages l ON l.id = lx.language_id
        GROUP BY l.code, l.script
        ORDER BY l.script = 'Latn', l.code
    """)).mappings().all()

    print(f"\n{'lang':5s} {'script':6s} {'total':>9s} {'filled':>9s} "
          f"{'pct':>6s} {'vis_total':>10s} {'vis_filled':>11s} "
          f"{'vis_pct':>8s} {'maxlen':>6s}")
    for r in rows:
        pct = 100 * r["filled"] / max(r["total"], 1)
        vpct = 100 * r["filled_visible"] / max(r["total_visible"], 1)
        print(f"{r['code']:5s} {str(r['script']):6s} {r['total']:9d} "
              f"{r['filled']:9d} {pct:5.1f}% {r['total_visible']:10d} "
              f"{r['filled_visible']:11d} {vpct:7.1f}% "
              f"{r['maxlen'] or 0:6d}")

    latn_filled = sum(r["filled"] for r in rows if r["script"] == "Latn")
    print(f"\nLatn-script rows with a romanization: {latn_filled} "
          f"(MUST be 0 -- non-zero means the script gate leaked)")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--lang", default=None, help="comma-separated ISO codes")
    ap.add_argument("--source", default="kaikki",
                    choices=["kaikki", "algorithmic", "both"])
    ap.add_argument("--force", action="store_true",
                    help="re-derive rows that already have a value")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--report", action="store_true",
                    help="print coverage only, derive nothing")
    args = ap.parse_args()

    codes = args.lang.split(",") if args.lang else None

    with SessionLocal() as db:
        db.execute(text("SET lock_timeout = '30s'"))

        if args.report:
            report(db)
            return

        langs = target_languages(db, codes)
        print(f"targets ({args.source}): "
              f"{', '.join(f'{l.code}/{l.script}' for l in langs)}")

        totals: dict[str, int] = {}
        for lang in langs:
            print(f"  {lang.code} ({lang.script}) ...")
            stats = backfill_language(
                db, lang, args.source, args.force, args.dry_run
            )
            for key, value in stats.items():
                totals[key] = totals.get(key, 0) + value
            print(f"    kaikki={stats['kaikki']} "
                  f"algo={stats['algorithmic']} blank={stats['blank']} "
                  f"overlong={stats['overlong_discarded']} "
                  f"written={stats['written']}")

        print(f"\nTOTALS: {totals}")
        if args.dry_run:
            print("(dry run -- nothing written)")
        else:
            report(db)


if __name__ == "__main__":
    main()