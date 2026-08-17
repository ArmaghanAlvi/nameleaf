"""
Phase C instrumentation (roadmap C3).

Times ONE parallel_expand call and attributes the wall clock to: root
acquisition, per-tree traversal, embedding inference, and SQL -- with counts,
not just totals.

WHY BEFORE ANY FIX: every item in C4 and C5 is ranked from code reading. The
project's standing note flags "mechanism proposal outrunning measurement" as a
recorded anti-pattern; this is the instrument that stops it here.

READ-ONLY. It calls parallel_expand directly rather than the route, and the
route is what writes SenseSelectionStat -- so re-running this does NOT move
the diff_reference baseline.

Reports TWO runs: COLD (module caches empty, so _language_order and
_pivot_eligible_languages both fire) and WARM. The delta between them IS the
post-restart warm-up cost the roadmap estimates at ~35s.

USAGE (from backend/):
  python3 scripts/eval/phase_timing.py --word light --width 3 --depth 3
"""
from __future__ import annotations

import argparse
import contextlib
import os
import sys
import time
from collections import Counter, defaultdict

sys.path.insert(0, os.getcwd())

from sqlalchemy import event                                          # noqa: E402

from app.db.session import SessionLocal, engine                       # noqa: E402
import app.services.embedding_provider as ep                          # noqa: E402
import app.services.parallel_expansion as px                          # noqa: E402
import app.services.vector_sense_search as vss                        # noqa: E402
from scripts.eval.capture_engine_reference import most_used_sense_id  # noqa: E402


SQL_N: Counter[str] = Counter()
SQL_T: dict[str, float] = defaultdict(float)
_started: dict[int, float] = {}


@event.listens_for(engine, "before_cursor_execute")
def _before(conn, cursor, statement, parameters, context, executemany):
    _started[id(context)] = time.perf_counter()


@event.listens_for(engine, "after_cursor_execute")
def _after(conn, cursor, statement, parameters, context, executemany):
    t0 = _started.pop(id(context), None)
    if t0 is None:
        return
    shape = " ".join(statement.split())[:120]
    SQL_N[shape] += 1
    SQL_T[shape] += time.perf_counter() - t0


EMBED = {"n": 0, "t": 0.0}
PHASE: dict[str, dict[str, float]] = defaultdict(lambda: {"n": 0.0, "t": 0.0})


def _instrument() -> None:
    real_embed = ep.embed_query

    def timed_embed(text: str):
        t0 = time.perf_counter()
        try:
            return real_embed(text)
        finally:
            EMBED["n"] += 1
            EMBED["t"] += time.perf_counter() - t0

    # BOTH bindings. expansion.py and multi_hop_expansion.py import embed_query
    # INSIDE the function body, so they resolve it from the module at call time
    # and patching the module is enough. vector_sense_search binds it at module
    # import time and needs its own patch.
    ep.embed_query = timed_embed
    vss.embed_query = timed_embed

    def wrap(module, name: str, label: str) -> None:
        real = getattr(module, name)

        def inner(*args, **kwargs):
            t0 = time.perf_counter()
            try:
                return real(*args, **kwargs)
            finally:
                PHASE[label]["n"] += 1
                PHASE[label]["t"] += time.perf_counter() - t0

        setattr(module, name, inner)

    # Patch the names as PARALLEL_EXPANSION sees them: it does
    # `from app.services.root_selection import select_root, ...`, so patching
    # the defining module would have no effect on its call sites.
    wrap(px, "select_roots", "root: ladder (all languages)")
    wrap(px, "select_root", "root: single (llm retry path)")
    wrap(px, "_pivot_root_rescue", "root: pivot rescue")
    wrap(px, "vector_fallback_root", "root: vector fallback")
    wrap(px, "multi_hop_expand", "traversal: multi_hop_expand")
    wrap(px, "_pivot_top_up", "traversal: pivot top-up")


def _reset_counters() -> None:
    SQL_N.clear()
    SQL_T.clear()
    PHASE.clear()
    EMBED["n"] = 0
    EMBED["t"] = 0.0


def _reset_module_caches() -> None:
    """Force the next call to pay the warm-up, so COLD is reproducible in a
    single process. Post-C1 the language directory owns one of the two."""
    from app.services import language_directory
    language_directory.reset_caches()
    px.reset_caches()


def _report(title: str, wall: float, trees: int, nodes: int) -> None:
    sql_t = sum(SQL_T.values())
    sql_n = sum(SQL_N.values())
    print(f"\n=== {title} ===")
    print(f"wall          {wall:8.2f}s     trees={trees} nodes={nodes}")
    print(f"embed         {EMBED['t']:8.2f}s  n={EMBED['n']:4d}  "
          f"{EMBED['t'] / max(EMBED['n'], 1) * 1000:6.1f} ms/call  "
          f"{EMBED['t'] / wall * 100:5.1f}% of wall")
    print(f"sql           {sql_t:8.2f}s  n={sql_n:4d}  "
          f"{sql_t / wall * 100:5.1f}% of wall")
    print(f"python (rest) {wall - EMBED['t'] - sql_t:8.2f}s")

    print("\n-- phases (NESTED: select_roots contains select_root's work) --")
    for label, d in sorted(PHASE.items(), key=lambda kv: -kv[1]["t"]):
        print(f"  {label:38s} {d['t']:7.2f}s  n={int(d['n'])}")

    print("\n-- top 12 SQL shapes by total time --")
    for shape, t in sorted(SQL_T.items(), key=lambda kv: -kv[1])[:12]:
        print(f"  {t:6.2f}s  n={SQL_N[shape]:4d}  {shape}")

    print("\n-- top 6 SQL shapes by COUNT --")
    for shape, n in SQL_N.most_common(6):
        print(f"  n={n:4d}  {SQL_T[shape]:6.2f}s  {shape}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--word", default="light")
    ap.add_argument("--width", type=int, default=3)
    ap.add_argument("--depth", type=int, default=3)
    args = ap.parse_args()

    _instrument()

    with SessionLocal() as db:
        sid = most_used_sense_id(db, args.word)
        if sid is None:
            print(f"no embedded visible sense for {args.word!r}")
            return
        print(f"probe word={args.word!r} sense_id={sid} "
              f"width={args.width} depth={args.depth}")

        # Warm the model OUTSIDE the measured runs: the first MPS forward pass
        # includes graph compilation and would otherwise be charged to embed.
        ep.get_model()
        ep.embed_query("warmup")

        for label in ("COLD (module caches empty)", "WARM"):
            if label.startswith("COLD"):
                _reset_module_caches()
            _reset_counters()
            t0 = time.perf_counter()
            with open(os.devnull, "w") as dn, contextlib.redirect_stdout(dn):
                result = px.parallel_expand(
                    db, english_sense_id=sid, language_codes=None,
                    width=args.width, depth=args.depth,
                    min_length=0, max_length=30,
                )
            _report(label, time.perf_counter() - t0,
                    len(result.trees), len(result.interleaved))


if __name__ == "__main__":
    main()