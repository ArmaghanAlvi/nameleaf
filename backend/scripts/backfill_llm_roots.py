"""
Bounded, resumable TRIAGE pass over the thin population (no curated link, no
shared ILI, no prior attempt). Breakdown 4.5 Step 7, inverted for batching in
Breakdown I Step 4.

⟲ SUPERSEDED: this script's original docstring, and decision 1d, called the
backfill the PRIMARY llm-rung mechanism. G-2 falsified that -- 4,024 of
10,785,974 thin pairs is 0.037%, so completion is ~1 year batched and ~20
unbatched. This is a TRIAGE tool: it fills the senses users are most likely to
reach, and is permanently incomplete by design. The query-time trickle
(Stage 17) is the mechanism that can actually keep up.

BATCHED. One API call per SENSE, covering every language that sense is thin
in, instead of one call per (sense, language) pair. --limit still means API
CALLS, because that is the quota unit; pair coverage is reported separately
so the relationship to the daily cap stays legible.

_THIN_SQL's ordering is doing more work than its original comment credited it
with: it is the only thing deciding which 0.037% ever gets filled. Tier 1 is
recorded dropdown selections; tier 2 is translation breadth on the English
entry across ALL Kaikki-documented languages, a proxy for "common headword"
vs. long-tail sense.

USAGE (from backend/):
  python3 scripts/backfill_llm_roots.py --dry-plan --limit 20
  python3 scripts/backfill_llm_roots.py --limit 100
  python3 scripts/backfill_llm_roots.py --targets la ru ja ar --limit 100
  python3 scripts/backfill_llm_roots.py --errors-only --limit 100
"""
from __future__ import annotations

import argparse, os, sys
from collections import Counter

from sqlalchemy import text

sys.path.insert(0, os.getcwd())

from app.db.session import SessionLocal             # noqa: E402
from app.services.root_llm import resolve_llm_roots  # noqa: E402

# Phase 1: the candidate sense pool, ordered exactly as the per-language
# _THIN_SQL used to order. OVERSAMPLED (--pool), because a sense high in this
# order may have zero thin languages left, and filtering after the fact would
# silently shrink the session below --limit.
_POOL_SQL = text("""
CREATE TEMP TABLE tmp_llm_pool AS
SELECT p.sense_id,
       COALESCE(sel.selection_count, 0) AS sel_count,
       COALESCE(json_array_length(p.raw_entry->'translations'), 0)
           AS tr_breadth
FROM (
  SELECT s.id AS sense_id, lx.raw_entry AS raw_entry
  FROM senses s
  JOIN lexemes lx ON lx.id = s.lexeme_id
  JOIN sense_embeddings se ON se.sense_id = s.id
  WHERE lx.language_id = 1 AND s.visibility_status = 'visible'
) p
LEFT JOIN sense_selection_stats sel ON sel.sense_id = p.sense_id
ORDER BY (COALESCE(sel.selection_count, 0) = 0) ASC,
         COALESCE(sel.selection_count, 0) DESC,
         COALESCE(json_array_length(p.raw_entry->'translations'), 0) DESC,
         p.sense_id
LIMIT :pool
""")

# Phase 2: thin PAIRS, but only for the materialized pool. The three NOT
# EXISTS predicates are unchanged from the original _THIN_SQL; what changed
# is that they run against a small indexed temp table instead of the full
# sense set once per language. This is the recorded remedy for correlated
# subqueries hanging on large tables -- materialize the candidate set first.
_THIN_PAIRS_SQL = text("""
CREATE TEMP TABLE tmp_llm_thin AS
SELECT p.sense_id, l.id AS language_id
FROM tmp_llm_pool p
CROSS JOIN (SELECT id FROM languages WHERE code = ANY(:codes)) l
WHERE NOT EXISTS (
        SELECT 1 FROM sense_translations st
        JOIN senses ds ON ds.lexeme_id = st.target_lexeme_id
        JOIN sense_embeddings de ON de.sense_id = ds.id
        WHERE st.sense_id = p.sense_id AND st.language_id = l.id
          AND st.target_lexeme_id IS NOT NULL
          AND ds.visibility_status = 'visible')
  AND NOT EXISTS (
        SELECT 1 FROM sense_synsets ss1
        JOIN sense_synsets ss2 ON ss2.ili = ss1.ili
        JOIN senses ts ON ts.id = ss2.sense_id
        JOIN lexemes tl ON tl.id = ts.lexeme_id
        JOIN sense_embeddings te ON te.sense_id = ts.id
        WHERE ss1.sense_id = p.sense_id AND tl.language_id = l.id
          AND ts.visibility_status = 'visible')
  AND NOT EXISTS (
        SELECT 1 FROM root_llm_attempts a
        WHERE a.sense_id = p.sense_id AND a.language_id = l.id
          AND a.status = ANY(:skip_statuses))
""")

_WORK_SQL = text("""
SELECT t.sense_id,
       array_agg(t.language_id ORDER BY t.language_id) AS lang_ids
FROM tmp_llm_thin t
JOIN tmp_llm_pool p ON p.sense_id = t.sense_id
GROUP BY t.sense_id, p.sel_count, p.tr_breadth
ORDER BY (p.sel_count = 0) ASC, p.sel_count DESC,
         p.tr_breadth DESC, t.sense_id
LIMIT :limit
""")

# Stage 16a diagnostics, batched: retry EXACTLY the errored pairs, grouped by
# sense so one call covers every errored language on that sense.
_ERRORS_SQL = text("""
SELECT a.sense_id, array_agg(a.language_id ORDER BY a.language_id) AS lang_ids
FROM root_llm_attempts a
JOIN languages l ON l.id = a.language_id
WHERE a.status = 'error' AND l.code = ANY(:codes)
GROUP BY a.sense_id
ORDER BY a.sense_id
LIMIT :limit
""")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--targets", nargs="+", default=None,
                    help="ISO codes; default is every non-English language")
    ap.add_argument("--limit", type=int, default=100,
                    help="API CALLS this session (one call == one sense)")
    ap.add_argument("--pool", type=int, default=None,
                    help="candidate senses to consider; default limit * 4")
    ap.add_argument("--retry-errors", action="store_true")
    ap.add_argument("--errors-only", action="store_true")
    ap.add_argument("--dry-plan", action="store_true",
                    help="print the work list and exit; no API calls")
    args = ap.parse_args()

    skip = ["resolved", "unresolved"] + ([] if args.retry_errors
                                         else ["error"])
    pool = args.pool or args.limit * 4

    with SessionLocal() as db:
        db.execute(text("SET lock_timeout = '30s'"))
        # Bounds disk spill if the planning query is cancelled mid-flight
        # (recorded gotcha); scoped to this session only.
        db.execute(text("SET temp_file_limit = '2GB'"))

        codes = args.targets or [c for (c,) in db.execute(text(
            "SELECT code FROM languages "
            "WHERE code IS NOT NULL AND code <> 'en' ORDER BY code"))]
        id_to_code = {i: c for i, c in db.execute(text(
            "SELECT id, code FROM languages WHERE code = ANY(:c)"),
            {"c": codes})}
        print(f"targets ({len(codes)}): {codes}")

        if args.errors_only:
            work = [(sid, list(ids)) for sid, ids in db.execute(
                _ERRORS_SQL, {"codes": codes, "limit": args.limit})]
        else:
            db.execute(text("DROP TABLE IF EXISTS tmp_llm_thin"))
            db.execute(text("DROP TABLE IF EXISTS tmp_llm_pool"))
            db.execute(_POOL_SQL, {"pool": pool})
            db.execute(text("CREATE INDEX ON tmp_llm_pool (sense_id)"))
            db.execute(text("ANALYZE tmp_llm_pool"))
            db.execute(_THIN_PAIRS_SQL,
                       {"codes": codes, "skip_statuses": skip})
            db.execute(text("CREATE INDEX ON tmp_llm_thin (sense_id)"))
            db.execute(text("ANALYZE tmp_llm_thin"))
            work = [(sid, list(ids)) for sid, ids in db.execute(
                _WORK_SQL, {"limit": args.limit})]
            # Read into Python and DROP before the resolve loop starts:
            # resolve_llm_roots commits, and a commit is where temp-table
            # lifetime assumptions go wrong.
            db.execute(text("DROP TABLE IF EXISTS tmp_llm_thin"))
            db.execute(text("DROP TABLE IF EXISTS tmp_llm_pool"))
        db.commit()

        pairs = sum(len(ids) for _s, ids in work)
        print(f"plan: {len(work)} calls covering {pairs} pairs "
              f"({pairs / max(len(work), 1):.2f} pairs/call)")
        if args.dry_plan:
            for sid, ids in work[:20]:
                print(f"  sense={sid:<8} langs="
                      f"{[id_to_code[i] for i in ids]}")
            return

        tally: Counter = Counter()
        calls = 0
        for sid, ids in work:
            langs = [id_to_code[i] for i in ids]
            res = resolve_llm_roots(db, english_sense_id=sid,
                                    language_codes=langs)
            calls += 1
            for code, lex in res.items():
                tally[f"{code}:{'resolved' if lex else 'miss'}"] += 1
            if calls % 10 == 0:
                print(f"[{calls}/{len(work)}] pairs={sum(tally.values())}",
                      flush=True)

    done = sum(tally.values())
    print(f"\nfinal: calls={calls} pairs={done} "
          f"fanout={done / max(calls, 1):.2f}")
    for k in sorted(tally):
        print(f"  {k:<16} {tally[k]}")


if __name__ == "__main__":
    print("NOTE: senses/lexemes/glosses sent to the configured LLM API.")
    main()