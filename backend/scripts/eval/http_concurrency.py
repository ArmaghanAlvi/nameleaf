"""
Option B real-HTTP concurrency confirmation (roadmap Stage 7c; deferred in
EXPANSION_FEATURE_COMPLETE_RECORD 32). Fires real POST /explore-v2 requests
at a RUNNING uvicorn and reports throughput + latency percentiles per
concurrency level, in two panels:
  en-only        comparable to the Option-A ~2.6-2.8/s ceiling
  all languages  the new N-tree load shape (the number that matters now)

PREREQS: uvicorn running exactly as in dev (single worker); the SERVER must
be started with ROOT_LLM_QUERY_TIME=0 -- this script talks to a separate
process, so fence_query_time_llm() cannot reach it, and after Stage 17d the
default is ON. Every other harness fences itself; this one cannot, which is
why it is on the tests/test_llm_fence.py exemption list. Warm
the MPS model implicitly via the warmup pass below.

USAGE (from backend/): python3 scripts/eval/http_concurrency.py
"""
from __future__ import annotations

import argparse
import time
from concurrent.futures import ThreadPoolExecutor

import httpx

WORDS = ["brave", "light", "storm", "river", "calm",
         "joy", "shadow", "fierce", "gold", "whisper"]
LEVELS = [1, 2, 4, 8]
REQUESTS_PER_LEVEL = 20


def sense_ids(base: str, words: list[str]) -> list[int]:
    out = []
    with httpx.Client(timeout=30) as c:
        for w in words:
            r = c.get(f"{base}/senses/lookup",
                      params={"query": w, "languageCode": "en", "limit": "1"})
            r.raise_for_status()
            opts = r.json()["options"]
            if opts:
                out.append(opts[0]["senseId"])
    return out


def one(base: str, sid: int, codes: list[str] | None,
        depth: int = 2) -> tuple[float, int]:
    body = {"selectedSenseIds": [sid], "queryText": "",
            "expansionCount": 3, "width": 3, "depth": depth,
            "language": None, "languageCodes": codes,
            "minLength": 0, "maxLength": 30}
    t0 = time.perf_counter()
    with httpx.Client(timeout=180) as c:
        r = c.post(f"{base}/explore-v2", json=body)
    return time.perf_counter() - t0, r.status_code


def run_level(base: str, sids: list[int], codes: list[str] | None,
              c_level: int, depth: int) -> None:
    tasks = [sids[i % len(sids)] for i in range(REQUESTS_PER_LEVEL)]
    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=c_level) as ex:
        results = list(ex.map(lambda sid: one(base, sid, codes, depth), tasks))
    wall = time.perf_counter() - t0
    lats = sorted(dt for dt, _ in results)
    errors = sum(1 for _, s in results if s != 200)
    p50 = lats[len(lats) // 2]
    p95 = lats[max(int(len(lats) * 0.95) - 1, 0)]
    print(f"  c={c_level:<2} n={REQUESTS_PER_LEVEL} "
          f"throughput={REQUESTS_PER_LEVEL / wall:5.2f}/s "
          f"p50={p50:6.2f}s p95={p95:6.2f}s errors={errors}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    ap.add_argument("--depth", type=int, default=2)
    ap.add_argument("--words", nargs="+", default=None)
    args = ap.parse_args()
    words = args.words or WORDS

    sids = sense_ids(args.base, words)
    all_codes = [l["code"] for l in
                 httpx.get(f"{args.base}/languages", timeout=30).json()]

    # 5 warmups: the first run showed c=1 slower than c=2 (residual cold
    # start inflating the serial baseline). With --words brave, sids has one
    # element, so cycle rather than slice.
    print("warmup (serial, excluded from stats)...")
    for i in range(5):
        one(args.base, sids[i % len(sids)], all_codes, args.depth)

    print(f"\npanel 1: en-only (compare to Option-A ~2.6-2.8/s ceiling, "
          f"EXPANSION record 32)")
    for c in LEVELS:
        run_level(args.base, sids, ["en"], c, args.depth)

    print(f"\npanel 2: all languages {all_codes} (the new load shape)")
    for c in LEVELS:
        run_level(args.base, sids, all_codes, c, args.depth)


if __name__ == "__main__":
    main()