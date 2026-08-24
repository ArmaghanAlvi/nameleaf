"""
Stage 7 retrieval tests. Real ORM queries against the sqlite fixture -- the
service is written in Core precisely so this is possible, since retrieval is
the only green-card code that runs per request.
"""
from dataclasses import dataclass

from app.models.generated_name import Language
from app.models.semantic import (
    EstablishedName,
    EstablishedNameCluster,
    EstablishedNameToken,
    Lexeme,
    Sense,
    Source,
)
from app.services.green_card_retrieval import (
    MECH_HOMOGRAPH,
    MECH_TOKEN,
    retrieve_green_cards,
)


@dataclass
class FakeNode:
    """Stand-in for HopNode: retrieval reads only `.sense` and `.depth`."""
    sense: Sense
    depth: int


def seed(db):
    src = Source(name="t", source_type="t")
    en = Language(name="English", code="en", script="Latn")
    hi = Language(name="Hindi", code="hi", script="Deva")
    db.add_all([src, en, hi])
    db.flush()
    return src, en, hi


def add_lex(db, src, lang, lemma, norm, pos="noun"):
    lx = Lexeme(language_id=lang.id, lemma=lemma, normalized_lemma=norm,
                part_of_speech=pos, source_id=src.id,
                source_entry_id=f"e-{lemma}", raw_entry={})
    db.add(lx)
    db.flush()
    s = Sense(lexeme_id=lx.id, source_id=src.id,
              source_locator=f"e-{lemma}:0", sense_index=0,
              definition=f"{lemma} def")
    db.add(s)
    db.flush()
    return lx, s


def add_name(db, lang, lemma, norm, ntype="given", tokens=(), cluster=None,
             popularity=None, src=None, lex=None, sense=None,
             confidence="corroborated"):
    assert lex is not None
    assert sense is not None
    n = EstablishedName(
        language_id=lang.id, lemma=lemma, normalized_lemma=norm,
        name_type=ntype, gender="u", source_lexeme_id=lex.id,
        source_sense_id=sense.id, cluster_id=cluster,
        popularity_rank=popularity, homograph_confidence=confidence,
    )
    db.add(n)
    db.flush()
    for t in tokens:
        db.add(EstablishedNameToken(established_name_id=n.id, token=t))
    db.flush()
    return n


# --- mechanism 1 -----------------------------------------------------------

def test_mechanism_one_joins_on_token(db):
    src, en, hi = seed(db)
    lx, s = add_lex(db, src, en, "light", "light")
    hlx, hs = add_lex(db, src, hi, "आकाश", "आकाश")
    add_name(db, hi, "आकाश", "आकाश", tokens=["sky", "light"],
             lex=hlx, sense=hs)
    cards = retrieve_green_cards(
        db, english_nodes=[FakeNode(s, 0)], visible_nodes=[],
        language_codes=["hi"])
    assert [c.name.lemma for c in cards] == ["आकाश"]
    assert cards[0].mechanisms == frozenset({MECH_TOKEN})
    assert cards[0].is_gradient is False


def test_token_match_is_exact_not_substring(db):
    # 7b's stated reason for normalize_lemma over LIKE.
    src, en, hi = seed(db)
    lx, s = add_lex(db, src, en, "light", "light")
    hlx, hs = add_lex(db, src, hi, "क", "क")
    add_name(db, hi, "Delight", "delight", tokens=["delight"],
             lex=hlx, sense=hs)
    assert retrieve_green_cards(
        db, english_nodes=[FakeNode(s, 0)], visible_nodes=[],
        language_codes=["hi"]) == []


def test_mechanism_one_is_independent_of_english_visibility(db):
    # The invariant the forced pass exists to create: the same query returns
    # the same names whether or not English is displayed.
    src, en, hi = seed(db)
    lx, s = add_lex(db, src, en, "light", "light")
    hlx, hs = add_lex(db, src, hi, "आकाश", "आकाश")
    add_name(db, hi, "आकाश", "आकाश", tokens=["light"], lex=hlx, sense=hs)
    hidden = retrieve_green_cards(
        db, english_nodes=[FakeNode(s, 0)], visible_nodes=[],
        language_codes=["hi"])
    shown = retrieve_green_cards(
        db, english_nodes=[FakeNode(s, 0)], visible_nodes=[FakeNode(s, 0)],
        language_codes=["hi", "en"])
    assert [c.name.id for c in hidden] == [c.name.id for c in shown]
    assert hidden[0].trigger.visible is False
    assert shown[0].trigger.visible is True


# --- mechanism 2 and the gradient merge ------------------------------------

def test_gradient_when_the_word_is_visible(db):
    src, en, hi = seed(db)
    lx, s = add_lex(db, src, en, "light", "light")
    hlx, hs = add_lex(db, src, hi, "आकाश", "आकाश")
    add_name(db, hi, "आकाश", "आकाश", tokens=["light"], lex=hlx, sense=hs)
    cards = retrieve_green_cards(
        db, english_nodes=[FakeNode(s, 0)],
        visible_nodes=[FakeNode(hs, 1)], language_codes=["hi"])
    assert cards[0].is_gradient is True
    assert cards[0].mechanisms == {MECH_TOKEN, MECH_HOMOGRAPH}
    assert cards[0].anchor_sense_id == hs.id


def test_spelling_only_homograph_does_not_merge(db):
    # The Lucius case: same spelling is not same object.
    src, en, hi = seed(db)
    lx, s = add_lex(db, src, en, "light", "light")
    hlx, hs = add_lex(db, src, hi, "आकाश", "आकाश")
    add_name(db, hi, "आकाश", "आकाश", tokens=["light"], lex=hlx, sense=hs,
             confidence="spelling_only")
    cards = retrieve_green_cards(
        db, english_nodes=[FakeNode(s, 0)],
        visible_nodes=[FakeNode(hs, 1)], language_codes=["hi"])
    assert cards[0].is_gradient is False
    assert cards[0].anchor_sense_id == hs.id


def test_unrequested_language_is_never_returned(db):
    src, en, hi = seed(db)
    lx, s = add_lex(db, src, en, "light", "light")
    hlx, hs = add_lex(db, src, hi, "आकाश", "आकाश")
    add_name(db, hi, "आकाश", "आकाश", tokens=["light"], lex=hlx, sense=hs)
    assert retrieve_green_cards(
        db, english_nodes=[FakeNode(s, 0)], visible_nodes=[],
        language_codes=["ru"]) == []


# --- trigger attribution ---------------------------------------------------

def test_hidden_trigger_yields_a_top_level_anchor(db):
    src, en, hi = seed(db)
    lx, s = add_lex(db, src, en, "light", "light")
    hlx, hs = add_lex(db, src, hi, "आकाश", "आकाश")
    hlx2, hs2 = add_lex(db, src, hi, "प्रकाश", "प्रकाश")
    add_name(db, hi, "आकाश", "आकाश", tokens=["light"], lex=hlx, sense=hs)
    cards = retrieve_green_cards(
        db, english_nodes=[FakeNode(s, 0)],
        visible_nodes=[FakeNode(hs2, 0)], language_codes=["hi"])
    assert cards[0].trigger.visible is False
    assert cards[0].trigger.lemma == "light"
    assert cards[0].anchor_sense_id is None


# --- ordering, dedup, caps -------------------------------------------------

def test_given_sorts_before_surname(db):
    src, en, hi = seed(db)
    lx, s = add_lex(db, src, en, "light", "light")
    elx, es = add_lex(db, src, en, "x", "x")
    add_name(db, en, "Zed", "zed", "given", tokens=["light"],
             lex=elx, sense=es)
    add_name(db, en, "Abbot", "abbot", "surname", tokens=["light"],
             lex=elx, sense=es)
    cards = retrieve_green_cards(
        db, english_nodes=[FakeNode(s, 0)], visible_nodes=[],
        language_codes=["en"])
    assert [c.name.lemma for c in cards] == ["Zed", "Abbot"]


def test_tier_beats_type(db):
    # 7e's stated priority, encoded so a later reordering is a deliberate
    # amendment rather than a drift.
    src, en, hi = seed(db)
    lx, s = add_lex(db, src, en, "light", "light")
    lx2, s2 = add_lex(db, src, en, "glow", "glow")
    elx, es = add_lex(db, src, en, "x", "x")
    add_name(db, en, "Abbot", "abbot", "surname", tokens=["light"],
             lex=elx, sense=es)
    add_name(db, en, "Zed", "zed", "given", tokens=["glow"],
             lex=elx, sense=es)
    cards = retrieve_green_cards(
        db, english_nodes=[FakeNode(s, 0), FakeNode(s2, 2)],
        visible_nodes=[], language_codes=["en"])
    assert [c.name.lemma for c in cards] == ["Abbot", "Zed"]


def test_cluster_collapse_keeps_one_member(db):
    src, en, hi = seed(db)
    lx, s = add_lex(db, src, en, "pure", "pure")
    elx, es = add_lex(db, src, en, "x", "x")
    c = EstablishedNameCluster(name_type="given", size=2)
    db.add(c)
    db.flush()
    add_name(db, en, "Katherine", "katherine", tokens=["pure"], cluster=c.id,
             lex=elx, sense=es)
    add_name(db, en, "Kathryn", "kathryn", tokens=["pure"], cluster=c.id,
             lex=elx, sense=es)
    cards = retrieve_green_cards(
        db, english_nodes=[FakeNode(s, 0)], visible_nodes=[],
        language_codes=["en"])
    assert [c2.name.lemma for c2 in cards] == ["Katherine"]


def test_surname_cap(db):
    src, en, hi = seed(db)
    lx, s = add_lex(db, src, en, "light", "light")
    elx, es = add_lex(db, src, en, "x", "x")
    for i in range(12):
        add_name(db, en, f"S{i:02d}", f"s{i:02d}", "surname",
                 tokens=["light"], lex=elx, sense=es)
    cards = retrieve_green_cards(
        db, english_nodes=[FakeNode(s, 0)], visible_nodes=[],
        language_codes=["en"], per_token_cap=99, surname_cap=3)
    assert len(cards) == 3


def test_per_token_cap(db):
    src, en, hi = seed(db)
    lx, s = add_lex(db, src, en, "light", "light")
    elx, es = add_lex(db, src, en, "x", "x")
    for i in range(12):
        add_name(db, en, f"G{i:02d}", f"g{i:02d}", "given",
                 tokens=["light"], lex=elx, sense=es)
    cards = retrieve_green_cards(
        db, english_nodes=[FakeNode(s, 0)], visible_nodes=[],
        language_codes=["en"], per_token_cap=4)
    assert len(cards) == 4