"""
D8 -- observed display coverage. Reads a capture_parallel_reference.py JSON
and reports what fraction of the words that ACTUALLY APPEARED in results
carry a romanization, per language.

WHY THIS AND NOT JUST THE POPULATION REPORT: population coverage measures the
dictionary; this measures the screen. Search returns high-frequency lexemes,
and Kaikki romanization coverage correlates with entry richness, so the two
numbers can differ. The second is the one a user feels.

USAGE (from backend/):
  python3 scripts/prune/romanization_display_coverage.py /tmp/phaseD_px_after.json
"""
from __future__ import annotations

import json
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.getcwd())

from sqlalchemy import text                    # noqa: E402
from app.db.session import SessionLocal        # noqa: E402


def main() -> None:
    path = sys.argv[1]
    with open(path) as fh:
        data = json.load(fh)

    by_lang: dict[str, set[int]] = defaultdict(set)
    for cells in data["capture"].values():
        for cell in cells.values():
            for code, tree in cell["trees"].items():
                by_lang[code].update(tree.get("sense_ids") or [])

    with SessionLocal() as db:
        print(f"{'lang':5s} {'rows_shown':>11s} {'romanized':>10s} {'pct':>7s}")
        for code in sorted(by_lang):
            ids = list(by_lang[code])
            if not ids:
                continue
            row = db.execute(text("""
                SELECT count(*) AS n,
                       count(lx.romanization) AS filled,
                       l.script AS script
                FROM senses s
                JOIN lexemes lx ON lx.id = s.lexeme_id
                JOIN languages l ON l.id = lx.language_id
                WHERE s.id = ANY(:ids)
                GROUP BY l.script
            """), {"ids": ids}).mappings().first()
            if not row:
                continue
            flag = "  (Latn -- filled MUST be 0)" if row["script"] == "Latn" else ""
            pct = 100 * row["filled"] / max(row["n"], 1)
            print(f"{code:5s} {row['n']:11d} {row['filled']:10d} "
                  f"{pct:6.1f}%{flag}")


if __name__ == "__main__":
    main()