"""C2, cache plan Part A: search results never read usage statistics.

The reranker's score is the vector score plus definition-quality penalties
only, and a duplicate displayed word is resolved by vector score alone.
Plain stand-in objects carry the only Sense attributes the reranker reads.
"""
from __future__ import annotations

import inspect
from types import SimpleNamespace

import pytest

import app.services.sense_reranker as reranker
from app.services.sense_reranker import RerankCandidate, rerank_candidates
from app.services.vector_sense_search import _best_duplicate

# Specific (no generic prefix), long enough, no broad-domain term: no penalty.
GOOD_DEFINITION = "a courageous and determined person facing danger"


def _sense(sid: int, definition: str = GOOD_DEFINITION, synonym: bool = True):
    relations = [SimpleNamespace(relation_type="synonym")] if synonym else []
    return SimpleNamespace(id=sid, definition=definition, relations=relations)


def _cand(sid: int, score: float, **kw) -> RerankCandidate:
    return RerankCandidate(sense=_sense(sid, **kw), vector_score=score)


def test_rerank_takes_no_usage_statistics():
    assert "sense_selection_counts" not in inspect.signature(
        rerank_candidates).parameters
    with pytest.raises(TypeError):
        rerank_candidates(candidates=[], sense_selection_counts={1: 5})


def test_popularity_bonus_is_gone():
    for name in ("POPULAR_SENSE_BONUS", "popular_sense_keys_in_candidate_set",
                 "sense_popularity_bonus"):
        assert not hasattr(reranker, name)


def test_score_is_vector_plus_penalties_only():
    clean = rerank_candidates(candidates=[_cand(1, 0.9)])[0]
    assert clean.final_score == 0.9
    no_syn = rerank_candidates(candidates=[_cand(2, 0.9, synonym=False)])[0]
    assert no_syn.final_score == pytest.approx(0.9 + reranker.NO_SYNONYM_PENALTY)
    assert not any("popularity" in p for p in clean.explanation_parts)


def test_order_follows_score_not_identity():
    out = rerank_candidates(candidates=[_cand(1, 0.80), _cand(2, 0.95),
                                        _cand(3, 0.85)])
    assert [r.sense.id for r in out] == [2, 3, 1]


def test_duplicate_word_resolved_by_vector_score():
    group = [_cand(1, 0.81), _cand(2, 0.93), _cand(3, 0.88)]
    assert _best_duplicate(group).sense.id == 2


def test_exact_score_tie_goes_to_the_first_fetched():
    # Group order is fetch order (ORDER BY distance); max() keeps the first.
    group = [_cand(7, 0.90), _cand(4, 0.90), _cand(9, 0.80)]
    assert _best_duplicate(group).sense.id == 7
