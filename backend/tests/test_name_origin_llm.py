"""Prompt, schema, normalisation and reconciliation. NO NETWORK."""
from app.services import name_origin_llm as nol

VOCAB = ["Arabic", "English", "Old English", "Old Norse", "Russian", "other"]


def v(origin=None, display=None, raw=None, conf="high", coined=False):
    return nol.Verdict(origin=origin, display=display, raw=raw,
                       confidence=conf, is_coined=coined)


def norm(raw):
    return nol.normalize_origin(raw, VOCAB, "English")


def test_decline_tokens_normalise_to_none():
    for token in ("unknown", "  Unknown ", "n/a", ""):
        assert norm(token) == (None, None)


def test_ancestral_english_folds_for_english_host():
    assert norm("Old English") == ("English", "English")
    assert norm("Proto-Germanic") == ("English", "English")


def test_old_norse_is_not_folded():
    assert norm("Old Norse") == ("Old Norse", "Old Norse")


def test_fold_is_scoped_to_english_hosts():
    assert nol.normalize_origin("Old English", VOCAB, "Russian") == (
        "Old English", "Old English")


def test_out_of_vocabulary_keeps_its_string():
    assert norm("Turkish") == ("other", "Turkish")


def test_english_wins_a_two_pass_tie():
    r = nol.reconcile_ab(v("Arabic", "Arabic"), v("English", "English"),
                         host_language_name="English")
    assert (r.status, r.origin, r.agreement) == (
        "resolved", "English", "native")


def test_two_foreign_passes_agreeing_resolve():
    r = nol.reconcile_ab(v("Arabic", "Arabic"), v("Arabic", "Arabic"),
                         host_language_name="English")
    assert (r.status, r.origin, r.in_vocabulary) == ("resolved", "Arabic", True)


def test_two_different_others_are_not_agreement():
    r = nol.reconcile_ab(v("other", "Turkish"), v("other", "Kurdish"),
                         host_language_name="English")
    assert r.status == "disagreed"


def test_both_declining_is_unknown_not_disagreed():
    r = nol.reconcile_ab(v(), v(), host_language_name="English")
    assert (r.status, r.agreement) == ("unknown", "declined")


def test_one_declining_goes_to_pass_c():
    r = nol.reconcile_ab(v("Arabic", "Arabic"), v(),
                         host_language_name="English")
    assert r.status == "disagreed"


def test_pass_c_majority_resolves():
    r = nol.reconcile_c(v("Arabic", "Arabic"), v("Russian", "Russian"),
                        v("Arabic", "Arabic"))
    assert (r.status, r.origin, r.agreement) == ("resolved", "Arabic", "on_c")


def test_lone_english_third_vote_does_not_win():
    r = nol.reconcile_c(v("Arabic", "Arabic"), v("Russian", "Russian"),
                        v("English", "English"))
    assert (r.status, r.origin, r.agreement) == (
        "unknown", None, "no_majority")


def test_confidence_takes_the_weaker_pass():
    r = nol.reconcile_ab(v("Arabic", "Arabic", conf="high"),
                         v("Arabic", "Arabic", conf="low"),
                         host_language_name="English")
    assert r.confidence == "low"


def test_schema_declares_every_token_required():
    schema = nol.origin_schema(["i0", "i1"])
    assert schema["required"] == ["i0", "i1"]


def test_parse_raises_on_omission():
    import pytest
    with pytest.raises(ValueError):
        nol.parse_batch({"i0": {"origin": "Arabic", "confidence": "high",
                                "is_coined": False, "in_vocabulary": True}},
                        ["i0", "i1"], VOCAB, "English")


def test_prompt_is_byte_stable_across_key_order():
    a = nol.build_prompt("given", VOCAB, [("i0", {"name": "Amal",
                                                  "type": "given"})])
    b = nol.build_prompt("given", VOCAB, [("i0", {"type": "given",
                                                  "name": "Amal"})])
    assert a == b


def test_surname_prompt_states_the_floor():
    prompt = nol.build_prompt("surname", VOCAB, [("i0", {"name": "Smith"})])
    assert "Old English" in prompt and "Old Norse is NOT" in prompt


def test_patronymic_takes_the_surname_prompt():
    given = nol.build_prompt("given", VOCAB, [("i0", {"name": "X"})])
    patro = nol.build_prompt("patronymic", VOCAB, [("i0", {"name": "X"})])
    assert patro != given and "SURNAME" in patro