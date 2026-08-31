"""
Stage 17c. How much wall clock does the query-time trickle add?

DELIBERATELY NOT FENCED, and deliberately not part of any reference capture.
This is the ONE script whose whole purpose is to run parallel_expand with the
trickle LIVE, so it must not call fence_query_time_llm(). It writes no JSON,
produces no baseline, and nothing downstream reads its output -- which is
what makes it safe to leave unfenced while every harness stays fenced.

It is on tests/test_llm_fence.py's radar only because it names
parallel_expand; add it to _EXEMPT with this paragraph as the reason.

ATTRIBUTION. Three numbers per probe word:
  total        the whole parallel_expand call
  in_llm       wall inside resolve_llm_roots (HTTP round trip + the
               resolution loop, which grows from ~3 _display_sense_scored
               calls to as many as 3 x len(missing))
  remainder    total - in_llm - fenced_baseline, i.e. the extra select_roots
               over refilled codes

_last_call is reset before each word so EVERY measured call actually takes
the LLM call. That is the worst case, and the worst case is what the 10%
threshold is about -- requests where can_call_now() is False add nothing and
would flatter the average.

USAGE (from backend/, with ROOT_LLM_API_KEY set):
  python3 scripts/eval/trickle_latency.py --runs 3
  python3 scripts/eval/trickle_latency.py --runs 3 --fenced
"""
from __future__ import annotations

import argparse, os, statistics, sys, time

sys.path.insert(0, os.getcwd())

from sqlalchemy import text                                       # noqa: E402

from app.db.session import SessionLocal                           # noqa: E402
import app.services.root_llm as rl                                # noqa: E402
import app.services.parallel_expansion as px                      # noqa: E402
from scripts.eval.capture_engine_reference import most_used_sense_id  # noqa: E402

PROBE_WORDS = ["brave", "light", "storm", "river", "calm"]

_llm_wall = 0.0
_llm_calls = 0
_llm_langs = 0


def _instrument() -> None:
    """Wrap resolve_llm_roots to record its wall and its fan-out. Monkeypatch
    rather than editing parallel_expansion: measurement must not change the
    thing measured, and this script is the only caller that wants timing."""
    inner = px.resolve_llm_roots

    def timed(db, *, english_sense_id, language_codes):
        global _llm_wall, _llm_calls, _llm_langs
        t0 = time.perf_counter()
        out = inner(db, english_sense_id=english_sense_id,
                    language_codes=language_codes)
        _llm_wall += time.perf_counter() - t0
        _llm_calls += 1
        _llm_langs += len(language_codes)
        return out

    px.resolve_llm_roots = timed


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--fenced", action="store_true",
                    help="baseline arm: force the trickle off")
    args = ap.parse_args()

    if args.fenced:
        rl.fence_query_time_llm()
    elif not rl.query_time_live():
        print("ROOT_LLM_QUERY_TIME is not 1; rerun with it set, or use "
              "--fenced if you meant the baseline arm.")
        return
    _instrument()

    global _llm_wall, _llm_calls, _llm_langs
    per_word: dict[str, list[float]] = {w: [] for w in PROBE_WORDS}
    llm_per_word: dict[str, list[float]] = {w: [] for w in PROBE_WORDS}

    with SessionLocal() as db:
        db.execute(text("SET lock_timeout = '30s'"))
        db.execute(text("SET idle_in_transaction_session_timeout = '300s'"))
        sids = {w: most_used_sense_id(db, w) for w in PROBE_WORDS}
        for run in range(args.runs):
            for word in PROBE_WORDS:
                sid = sids[word]
                if sid is None:
                    continue
                _llm_wall, _llm_calls, _llm_langs = 0.0, 0, 0
                rl._last_call = 0.0        # force can_call_now() -> True
                t0 = time.perf_counter()
                px.parallel_expand(db, english_sense_id=sid,
                                   language_codes=None, width=3, depth=2,
                                   min_length=0, max_length=30)
                total = time.perf_counter() - t0
                per_word[word].append(total)
                llm_per_word[word].append(_llm_wall)
                print(f"run{run} {word:8s} total={total:7.2f}s "
                      f"in_llm={_llm_wall:6.2f}s calls={_llm_calls} "
                      f"langs={_llm_langs}", flush=True)

    print(f"\n--- medians over {args.runs} runs "
          f"({'FENCED' if args.fenced else 'LIVE'}) ---")
    totals, llms = [], []
    for word in PROBE_WORDS:
        if not per_word[word]:
            continue
        t = statistics.median(per_word[word])
        l = statistics.median(llm_per_word[word])
        totals.append(t)
        llms.append(l)
        print(f"  {word:8s} total={t:7.2f}s  in_llm={l:6.2f}s")
    print(f"  {'MEDIAN':8s} total={statistics.median(totals):7.2f}s  "
          f"in_llm={statistics.median(llms):6.2f}s")


if __name__ == "__main__":
    main()