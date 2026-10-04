"""
Harness patch check (B5, saved as a script in C2). No database.

capture_parallel_api_reference.py replaces the explore-v2 route's
`record_sense_selection` with a no-op at import time, so a route-level
capture can't write usage statistics to master. That only works while the
route calls the function by its MODULE-GLOBAL name. This imports the harness
and verifies the patch still intercepts the write, and reports the
direct-call policy the route will resolve under the current environment.

Exit status 1 if any check fails.

USAGE (from backend/):
  python3 scripts/eval/harness_patch_check.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.getcwd())

from app.services.root_llm import fence_query_time_llm  # noqa: E402

fence_query_time_llm()

import scripts.eval.capture_parallel_api_reference as harness  # noqa: E402
import app.api.routes.explore_v2 as route  # noqa: E402
from app.search_admission import resolve_policy  # noqa: E402


def main() -> None:
    patched = route.record_sense_selection
    checks = {
        "record_sense_selection is harness lambda":
            patched.__name__ == "<lambda>"
            and patched.__module__ == harness.__name__,
        "_run_search resolves via route module globals":
            route._run_search.__globals__ is vars(route),
        "_run_search body calls record_sense_selection by global name":
            "record_sense_selection" in route._run_search.__code__.co_names,
    }
    for name, ok in checks.items():
        print(f"{name}: {ok}")
    policy = resolve_policy(object())
    print(f"direct-call policy: limits_on={policy.limits_on} "
          f"admission_on={policy.admission is not None} "
          f"stats_write={getattr(policy, 'stats_write', 'n/a')}")
    if not all(checks.values()):
        sys.exit(1)


if __name__ == "__main__":
    main()
