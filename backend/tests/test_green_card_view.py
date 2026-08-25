"""
Stage 8 -- presentation and emission tests.

Real ORM queries against the sqlite fixture, same contract as
test_green_card_retrieval: the view service is written in Core so the whole
variant/cognate split is exercisable without Postgres.
"""
from dataclasses import dataclass

from app.models.generated_name import Language
from app.models.semantic import (
    EstablishedName,
    EstablishedNameCluster,
    EstablishedNameEdge,
    EstablishedNameToken,
    Lexeme,
    Sense,
    Source,
)
from app.services.green_card_retrieval import retrieve_green_cards
from app.services.green_card_view import build_views

from app.api.routes.explore_v2 import _attach_green_cards, _green_to_result
from app.schemas.explore_v2 import ExploreV2Result

@dataclass
class FakeNode:
    sense: Sense
    depth: int


def seed(db):
    src = Source(name="t", source_type="t")
    en = Language(name="English", code="en", script="Latn")
    de = Language(name="German", code="de", script="Latn")
    hi = Language(name="Hindi", code="hi", script="Deva")
    db.add_all([src, en, de, hi])
    db.flush()
    return src, en, de, hi


def add_lex(db, src, lang, lemma, norm, pos="noun"):
    lx = Lexeme(language_id=lang.id, lemma=lemma, normalized_lemma=norm,
                part_of_speech=pos, source_id=src.id,
                source_entry_id=f"e-{lang.code}-{lemma}", raw_entry={})
    db.add(lx)
    db.flush()
    s = Sense(lexeme_id=lx.id, source_id=src.id,
              source_locator=f"e-{lang.code}-{lemma}:0", sense_index=0,
              definition=f"{lemma} def")
    db.add(s)
    db.flush()
    return lx, s


def add_name(db, lang, lemma, norm, lex, sense, ntype="given", tokens=(),
             cluster=None, meaning=None, channel=None, confidence=None,
             homograph_lexeme_id=None, romanization=None):
    n = EstablishedName(
        language_id=lang.id, lemma=lemma, normalized_lemma=norm,
        name_type=ntype, gender="u", source_lexeme_id=lex.id,
        source_sense_id=sense.id, cluster_id=cluster,
        meaning_text=meaning, meaning_channel=channel,
        homograph_confidence=confidence,
        homograph_lexeme_id=homograph_lexeme_id,
        romanization=romanization,
    )
    db.add(n)
    db.flush()
    for t in tokens:
        db.add(EstablishedNameToken(established_name_id=n.id, token=t))
    db.flush()
    return n


def add_edge(db, source, target, relation, cross):
    db.add(EstablishedNameEdge(
        source_name_id=source.id, target_name_id=target.id,
        relation_type=relation, is_cross_language=cross))
    db.flush()


# --- the variant / cognate split ------------------------------------------

def test_same_language_cluster_members_are_variants_with_edge_labels(db):
    src, en, de, hi = seed(db)
    lx, s = add_lex(db, src, en, "pure", "pure")
    elx, es = add_lex(db, src, en, "x", "x", pos="name")
    c = EstablishedNameCluster(name_type="given", size=2)
    db.add(c)
    db.flush()
    kath = add_name(db, en, "Katharine", "katharine", elx, es,
                    tokens=["pure"], cluster=c.id)
    cathy = add_name(db, en, "Cathy", "cathy", elx, es, cluster=c.id)
    add_edge(db, cathy, kath, "DIMINUTIVE_OF", False)

    cards = retrieve_green_cards(db, english_nodes=[FakeNode(s, 0)],
                                 visible_nodes=[], language_codes=["en"])
    views = build_views(db, cards)
    assert [v.card.name.lemma for v in views] == ["Katharine"]
    v = views[0]
    assert [(x.lemma, x.relation, x.is_direct) for x in v.variants] == [
        ("Cathy", "Diminutive", True)]
    assert v.cognates == ()


def test_cross_language_equivalence_is_a_cognate_not_a_variant(db):
    # The load-bearing case: build_components unions SAME-LANGUAGE edges
    # only, so `cluster_id` can never carry a cognate. If this list came
    # from the cluster, 9c's second grouping would always be empty.
    src, en, de, hi = seed(db)
    lx, s = add_lex(db, src, en, "pure", "pure")
    elx, es = add_lex(db, src, en, "x", "x", pos="name")
    dlx, ds = add_lex(db, src, de, "y", "y", pos="name")
    kath = add_name(db, en, "Katharine", "katharine", elx, es, tokens=["pure"])
    kat = add_name(db, de, "Katharina", "katharina", dlx, ds)
    add_edge(db, kat, kath, "EQUIV_EN", True)

    views = build_views(db, retrieve_green_cards(
        db, english_nodes=[FakeNode(s, 0)], visible_nodes=[],
        language_codes=["en"]))
    v = views[0]
    assert v.variants == ()
    assert [(x.lemma, x.language_code, x.is_cross_language)
            for x in v.cognates] == [("Katharina", "de", True)]


def test_cognates_hang_off_the_family_not_only_the_matched_spelling(db):
    # 7e prefers "the member that actually matched", which is routinely NOT
    # the member Wiktionary glossed. Restricting the walk to the card would
    # empty the cognate list for exactly those cards.
    src, en, de, hi = seed(db)
    lx, s = add_lex(db, src, en, "pure", "pure")
    elx, es = add_lex(db, src, en, "x", "x", pos="name")
    dlx, ds = add_lex(db, src, de, "y", "y", pos="name")
    c = EstablishedNameCluster(name_type="given", size=2)
    db.add(c)
    db.flush()
    kathryn = add_name(db, en, "Kathryn", "kathryn", elx, es,
                       tokens=["pure"], cluster=c.id)
    kath = add_name(db, en, "Katherine", "katherine", elx, es, cluster=c.id)
    add_edge(db, kathryn, kath, "VARIANT_OF", False)
    kat = add_name(db, de, "Katharina", "katharina", dlx, ds)
    add_edge(db, kat, kath, "EQUIV_EN", True)   # points at the SIBLING

    views = build_views(db, retrieve_green_cards(
        db, english_nodes=[FakeNode(s, 0)], visible_nodes=[],
        language_codes=["en"]))
    v = views[0]
    assert v.card.name.lemma == "Kathryn"
    assert [x.lemma for x in v.variants] == ["Katherine"]
    assert [(x.lemma, x.is_direct) for x in v.cognates] == [
        ("Katharina", False)]


def test_variant_and_cognate_caps_report_the_true_total(db):
    src, en, de, hi = seed(db)
    lx, s = add_lex(db, src, en, "pure", "pure")
    elx, es = add_lex(db, src, en, "x", "x", pos="name")
    c = EstablishedNameCluster(name_type="given", size=6)
    db.add(c)
    db.flush()
    head = add_name(db, en, "Aaa", "aaa", elx, es, tokens=["pure"],
                    cluster=c.id)
    for i in range(5):
        add_name(db, en, f"V{i}", f"v{i}", elx, es, cluster=c.id)
    views = build_views(db, retrieve_green_cards(
        db, english_nodes=[FakeNode(s, 0)], visible_nodes=[],
        language_codes=["en"]), variant_cap=2)
    assert len(views[0].variants) == 2
    assert views[0].variant_total == 5


def test_spelling_only_label_never_asserts_meaning(db):
    src, en, de, hi = seed(db)
    lx, s = add_lex(db, src, en, "sky", "sky")
    wlx, ws = add_lex(db, src, hi, "आकाश", "आकाश")
    nlx, ns = add_lex(db, src, hi, "n", "n", pos="name")
    add_name(db, hi, "आकाश", "आकाश", nlx, ns, tokens=["sky"],
             meaning="sky", channel="HOMOGRAPH", confidence="spelling_only",
             homograph_lexeme_id=wlx.id)
    views = build_views(db, retrieve_green_cards(
        db, english_nodes=[FakeNode(s, 0)], visible_nodes=[],
        language_codes=["hi"]))
    label = views[0].provenance
    assert "Spelled identically" in label
    assert "आकाश" in label
    assert "means" not in label


def yellow(sense_id: int, name: str, depth: int = 0) -> ExploreV2Result:
    return ExploreV2Result(
        id=f"sense-{sense_id}", name=name, category="translation",
        meaning="d", language="Hindi", explanation="e",
        matchType="exact", matchedSenseId=sense_id,
        relationshipType="selected", relationshipWeight=1.0,
        partOfSpeech="noun", depth=depth,
    )


# --- emission -------------------------------------------------------------

def test_gradient_merges_onto_the_yellow_row_without_adding_one(db):
    src, en, de, hi = seed(db)
    lx, s = add_lex(db, src, en, "sky", "sky")
    wlx, ws = add_lex(db, src, hi, "आकाश", "आकाश")
    nlx, ns = add_lex(db, src, hi, "n", "n", pos="name")
    add_name(db, hi, "आकाश", "आकाश", nlx, ns, tokens=["sky"],
             confidence="corroborated")
    cards = retrieve_green_cards(
        db, english_nodes=[FakeNode(s, 0)],
        visible_nodes=[FakeNode(ws, 1)], language_codes=["hi"])
    rows = _attach_green_cards([yellow(ws.id, "आकाश", 1)],
                               build_views(db, cards))
    assert len(rows) == 1
    assert rows[0].category == "word-established"
    assert rows[0].id == f"sense-{ws.id}"     # tree position preserved
    assert rows[0].green is not None
    assert rows[0].green.isGradient is True


def test_only_one_gradient_merges_per_yellow_row(db):
    # `Martin` given + `Martin` surname are two rows at the
    # established_names grain, both gradient-eligible against one word.
    src, en, de, hi = seed(db)
    lx, s = add_lex(db, src, en, "sky", "sky")
    wlx, ws = add_lex(db, src, hi, "आकाश", "आकाश")
    nlx, ns = add_lex(db, src, hi, "n", "n", pos="name")
    add_name(db, hi, "आकाश", "आकाश", nlx, ns, tokens=["sky"],
             ntype="given", confidence="corroborated")
    add_name(db, hi, "आकाश", "आकाश", nlx, ns, tokens=["sky"],
             ntype="surname", confidence="corroborated")
    cards = retrieve_green_cards(
        db, english_nodes=[FakeNode(s, 0)],
        visible_nodes=[FakeNode(ws, 1)], language_codes=["hi"])
    rows = _attach_green_cards([yellow(ws.id, "आकाश", 1)],
                               build_views(db, cards))
    assert len(rows) == 2
    assert rows[0].category == "word-established"
    assert rows[0].green is not None
    assert rows[0].green.nameType == "given"      # better-ranked wins
    assert rows[1].category == "established"
    assert rows[1].green is not None
    assert rows[1].green.nameType == "surname"


def test_standalone_green_card_shape(db):
    src, en, de, hi = seed(db)
    lx, s = add_lex(db, src, en, "sky", "sky")
    nlx, ns = add_lex(db, src, hi, "n", "n", pos="name")
    n = add_name(db, hi, "आकाश", "आकाश", nlx, ns, tokens=["sky"],
                 meaning="sky", channel="GLOSS_MEANING",
                 romanization="aakaash")
    views = build_views(db, retrieve_green_cards(
        db, english_nodes=[FakeNode(s, 0)], visible_nodes=[],
        language_codes=["hi"]))
    row = _green_to_result(views[0])
    assert row.id == f"name-{n.id}"
    assert row.category == "established"
    assert row.partOfSpeech == "name"
    assert row.relationshipWeight is None
    assert row.matchedSenseId == ns.id      # the name's own sense
    assert row.romanization == "aakaash"
    assert row.green is not None
    assert row.green.matchedTokens == ["sky"]
    assert row.green.provenanceLabel.startswith("Meaning given")


def test_hidden_trigger_card_is_top_level(db):
    src, en, de, hi = seed(db)
    lx, s = add_lex(db, src, en, "sky", "sky")
    nlx, ns = add_lex(db, src, hi, "n", "n", pos="name")
    add_name(db, hi, "आकाश", "आकाश", nlx, ns, tokens=["sky"])
    views = build_views(db, retrieve_green_cards(
        db, english_nodes=[FakeNode(s, 0)], visible_nodes=[],
        language_codes=["hi"]))
    row = _green_to_result(views[0])
    assert row.parentSenseId is None
    assert row.green is not None
    assert row.green.triggerVisible is False
    assert row.green.triggerWord == "sky"
    assert "not shown in these results" in row.explanation