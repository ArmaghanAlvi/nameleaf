"""
Static guard: any harness or probe that can reach parallel_expand() must
fence the query-time LLM trickle.

WHY A GREP AND NOT A RUNTIME CHECK. The fence is a per-file opt-in and its
failure mode is silent -- a probe written six months from now that forgets
the call still runs, still prints numbers, and those numbers quietly contain
live LLM resolutions that no commit explains. Nothing at runtime can tell the
difference. A grep over the two script directories is the cheapest thing that
notices, and it costs no DB and no network.
"""
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
SCRIPT_DIRS = [BACKEND / "scripts" / "eval", BACKEND / "scripts" / "prune"]

# Ways a script can reach the trickle: parallel_expand directly, or the route
# with a languageCodes value (explore_v2 branches to parallel_expand on it).
# Deliberately CONSERVATIVE -- it matches comments and docstrings too, which
# is why the exemption list exists.
_REACHES = ("parallel_expand", "routes.explore_v2", "languageCodes")

# Files that name a trigger but provably cannot reach the trickle. Every entry
# carries its REASON, so the list cannot grow by habit.
_EXEMPT = {
    "capture_api_current.py":
        "no languageCodes -> explore_v2's legacy single-tree branch",
    "root_battery.py":
        "reads persisted root_llm_attempts rows; never calls the API",
    "http_concurrency.py":
        "out-of-process; the SERVER must start with ROOT_LLM_QUERY_TIME=0",
    "trickle_latency.py":
        "the ONE script whose purpose is to run the trickle live; writes no "
        "JSON and produces no baseline, so nothing downstream can inherit it",
}


def _candidates():
    for directory in SCRIPT_DIRS:
        for path in sorted(directory.glob("*.py")):
            if path.name == "__init__.py":
                continue
            src = path.read_text(encoding="utf-8", errors="replace")
            if any(t in src for t in _REACHES):
                yield path, src


def test_parallel_path_scripts_fence_the_llm():
    missing = [
        path.name for path, src in _candidates()
        if path.name not in _EXEMPT and "fence_query_time_llm()" not in src
    ]
    assert not missing, (
        "these scripts can reach parallel_expand() but never call "
        f"fence_query_time_llm(): {missing}. Add the call, or add the file "
        "to _EXEMPT with a reason."
    )


def test_every_exemption_still_matches_a_real_script():
    """An exemption for a file that no longer exists, or no longer names a
    trigger, is a stale reason nobody will re-read. Fail rather than let the
    list drift out of sync with what it is exempting."""
    names = {path.name for path, _ in _candidates()}
    stale = sorted(n for n in _EXEMPT if n not in names)
    assert not stale, f"exemption matches no current script: {stale}"


def test_the_guard_actually_finds_something():
    """A trigger list that matches nothing would make both tests above pass
    vacuously forever."""
    assert len(list(_candidates())) >= 10