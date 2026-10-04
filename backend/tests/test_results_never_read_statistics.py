"""
Static guard (C2, cache plan Part A): the search-RESULTS path never reads
usage statistics. Statistics shape the sense dropdown only
(services/sense_lookup.py, services/dropdown_ranker.py).

WHY A GREP: a statistics read on the results path makes results depend on
traffic -- unstable for the result cache, a bot-manipulation vector, and a
silent gate drift. Nothing at runtime notices it; a grep over the result-path
modules costs nothing and does.
"""
from pathlib import Path

SERVICES = Path(__file__).resolve().parent.parent / "app" / "services"

# Every module that builds search results (POST /explore-v2).
RESULT_PATH = (
    "vector_sense_search.py",
    "sense_reranker.py",
    "multi_hop_expansion.py",
    "expansion.py",
    "parallel_expansion.py",
    "root_selection.py",
    "green_card_retrieval.py",
    "green_card_view.py",
)

FORBIDDEN = (
    "sense_selection",
    "SenseSelectionStat",
    "SenseSelectionEvent",
    "get_sense_selection_counts",
    "word_search",
    "WordSearchStat",
    "WordSearchEvent",
    "selection_count",
)


def test_result_path_modules_exist():
    missing = [n for n in RESULT_PATH if not (SERVICES / n).exists()]
    assert not missing, f"result-path module list is stale: {missing}"


def test_result_path_reads_no_usage_statistics():
    offenders = {}
    for name in RESULT_PATH:
        src = (SERVICES / name).read_text(encoding="utf-8")
        hits = [t for t in FORBIDDEN if t in src]
        if hits:
            offenders[name] = hits
    assert not offenders, (
        "search results must not read usage statistics (CLAUDE.md): "
        f"{offenders}")
