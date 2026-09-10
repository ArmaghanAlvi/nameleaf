"""Breakdown K. The ancestral-English fold has two write sites as of
Step 2 -- normalize_origin (ledger tier) and apply_origin (category and
gloss_etym tiers). The SQL half cannot be unit-tested: apply_origin uses
PostgreSQL's ~* operator, which SQLite has no equivalent for, and the
conftest `db` fixture registers only char_length. See 23.2, where a
retracted test_origin_gloss_etym.py was replaced with real-corpus
verification for the same reason -- Step 2d is that verification here.

What IS testable, and is the real risk, is the two sites reading two
different lists. That is what 23.10's defect was, one level up: the fold
existed, it just wasn't reachable from where the writes happened.
"""
import importlib

from app.services import name_origin_llm as nol

VOCAB = ["Arabic", "English", "Old English", "Old Norse", "Russian", "other"]


def test_every_ancestral_member_folds_for_an_english_host():
    """The existing suite covers two members. This covers the set, so
    adding a member cannot silently ship untested."""
    for member in nol.ANCESTRAL_ENGLISH:
        assert nol.normalize_origin(member, VOCAB, "English") == (
            "English", "English"), member
        assert nol.normalize_origin(member.title(), VOCAB, "English") == (
            "English", "English"), member


def test_no_member_folds_for_a_non_english_host():
    """Old English is a truthful, in-vocabulary answer for a Russian row."""
    for member in nol.ANCESTRAL_ENGLISH:
        origin, _ = nol.normalize_origin(member, VOCAB, "Russian")
        assert origin != "Russian", member


def test_old_norse_and_frankish_are_not_in_the_set():
    """Danelaw surnames genuinely are Old Norse and `non` is a corpus
    language in its own right; Frankish is not an English ancestral
    stage. Both exclusions are deliberate -- see the constant's comment."""
    assert "old norse" not in nol.ANCESTRAL_ENGLISH
    assert "frankish" not in nol.ANCESTRAL_ENGLISH


def test_apply_origin_reads_the_same_object_not_a_copy():
    """THE POINT OF THIS FILE. A copied literal in the populate script
    would pass every test above and still leak the moment one list gained
    a member the other didn't."""
    pen = importlib.import_module("scripts.populate_established_names")
    assert pen.ANCESTRAL_ENGLISH is nol.ANCESTRAL_ENGLISH