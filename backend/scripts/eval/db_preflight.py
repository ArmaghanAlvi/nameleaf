"""
Read-only database pre-flight for gate runs (publishing C2).

Prints, through the app's own engine (no docker, no psql):
  * the usage-statistics fingerprint: count | sum(selection_count) |
    max(last_selected_at) over sense_selection_stats -- the ORM model's
    table, so it follows the table into schema `live` after C3's migration;
  * the established-names count (invariant: 106,398);
  * other client connections to this database (a gate needs none).
--dump-stat-ids FILE also writes the sense ids that have statistics rows,
for stats_trace.py.

Every statement runs in a READ ONLY transaction, so this cannot write even
by mistake.

USAGE (from backend/):
  python3 scripts/eval/db_preflight.py [--label TEXT] [--dump-stat-ids FILE]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime

sys.path.insert(0, os.getcwd())

from sqlalchemy import func, select, text  # noqa: E402

from app.db.session import engine  # noqa: E402
from app.models.semantic import SenseSelectionStat  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--label", default="")
    ap.add_argument("--dump-stat-ids")
    args = ap.parse_args()

    with engine.connect() as c:
        c.execute(text("SET TRANSACTION READ ONLY"))
        c.execute(text("SET lock_timeout = '30s'"))
        count, total, latest = c.execute(select(
            func.count(), func.sum(SenseSelectionStat.selection_count),
            func.max(SenseSelectionStat.last_selected_at),
        ).select_from(SenseSelectionStat)).one()
        est = c.execute(text("SELECT count(*) FROM established_names")).scalar()
        others = c.execute(text(
            "SELECT pid, application_name, state FROM pg_stat_activity "
            "WHERE backend_type = 'client backend' "
            "AND pid <> pg_backend_pid()")).all()
        ids = None
        if args.dump_stat_ids:
            ids = sorted(c.execute(select(SenseSelectionStat.sense_id)).scalars())
        c.rollback()

    print(f"== db_preflight {args.label} {datetime.now(UTC).isoformat()}")
    print(f"fingerprint: {count}|{total}|{latest}")
    print(f"established_names: {est}")
    print(f"other client backends: {len(others)} {[tuple(r) for r in others]}")
    if ids is not None:
        with open(args.dump_stat_ids, "w") as fh:
            json.dump(ids, fh)
        print(f"wrote {len(ids)} statistics sense ids to {args.dump_stat_ids}")


if __name__ == "__main__":
    main()
