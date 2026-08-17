"""Populate Language.has_wordnet_edges from the actual edge inventory.

WHY THIS EXISTS: pivot eligibility is "this language has ZERO wordnet synonym
edges." Computing it live costs ~17s per process, because the 14 languages
that genuinely have none cannot short-circuit -- proving a negative means
touching every sense of that language (measured: sw, 12,819 senses, 1,184ms).
Persisting the answer turns 20 existence probes into one cheap column read.
See notes/LATENCY_INVESTIGATION.md F11.

THE PROBE IS THE SAME ONE THE LIVE PATH USES, deliberately: this script must
produce exactly what _pivot_eligible_languages would have computed, or it
changes engine output. It imports the provenance set from the same registry
(app.utils.provenance.pivot_counting_provenances) rather than restating it --
a hardcoded list here would silently fork the day a wordnet is added.

RE-RUN THIS after any import that adds or removes wordnet edges, and after
adding a slug to WORDNET_PROVENANCES. The column is derived state; nothing
invalidates it automatically except the wordnet_lmf importer, which refreshes
only the single language it just imported.

English is included for completeness but is structurally irrelevant: it is the
pivot TARGET and parallel_expand never consults eligibility for it.

Dry-run by default; the dry run IS the measurement.
"""
import argparse
import os
import sys
import time

from sqlalchemy import select, text

sys.path.insert(0, os.getcwd())

from app.db.session import SessionLocal                      # noqa: E402
from app.models.generated_name import Language               # noqa: E402
from app.models.semantic import Lexeme, Sense, SenseRelation  # noqa: E402
from app.utils.provenance import pivot_counting_provenances  # noqa: E402


def has_wordnet_edge(db, language_id: int, provenances: tuple[str, ...]) -> bool:
    """Byte-for-byte the probe in parallel_expansion._has_wordnet_edge."""
    return db.scalar(
        select(Lexeme.id)
        .join(Sense, Sense.lexeme_id == Lexeme.id)
        .join(SenseRelation, SenseRelation.from_sense_id == Sense.id)
        .where(Lexeme.language_id == language_id,
               SenseRelation.provenance.in_(provenances))
        .limit(1)
    ) is not None


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true",
                    help="write changes; default is dry-run (measure only)")
    args = ap.parse_args()

    provenances = tuple(sorted(pivot_counting_provenances()))
    print(f"pivot-counting provenances ({len(provenances)}): "
          f"{', '.join(provenances)}\n")

    with SessionLocal() as db:
        db.execute(text("SET lock_timeout = '30s'"))

        rows = db.execute(
            select(Language.id, Language.code, Language.has_wordnet_edges)
            .where(Language.code.isnot(None))
            .order_by(Language.id)
        ).all()

        # --- READ PHASE: probe every language before writing anything --------
        computed: list[tuple[int, str, bool, bool | None]] = []
        for lang_id, code, stored in rows:
            t0 = time.perf_counter()
            value = has_wordnet_edge(db, lang_id, provenances)
            dt = (time.perf_counter() - t0) * 1000
            computed.append((lang_id, code, value, stored))
            state = "same" if stored is value else (
                "SET" if stored is None else "CHANGED")
            print(f"  {code:5s} has_wordnet_edges={str(value):5s} "
                  f"stored={str(stored):5s}  {state:7s} ({dt:7.1f}ms)")

        eligible = sorted(c for _i, c, v, _s in computed if not v and c != "en")
        print(f"\npivot-eligible (zero wordnet edges, excluding en): "
              f"{len(eligible)}")
        print(f"  {','.join(eligible)}")

        changed = [r for r in computed if r[3] is not r[2]]
        print(f"\nrows needing write: {len(changed)}")

        if not args.apply:
            print("\nDRY RUN -- nothing written. Re-run with --apply to commit.")
            return

        # --- WRITE PHASE -----------------------------------------------------
        for lang_id, code, value, _stored in changed:
            db.get(Language, lang_id).has_wordnet_edges = value
        db.commit()
        print(f"\nApplied: {len(changed)} language rows updated.")


if __name__ == "__main__":
    main()
