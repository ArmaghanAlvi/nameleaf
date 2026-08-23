from app.services.name_variants import (
    assert_cluster_ceiling,
    build_components,
    extract_edges,
    extract_target_candidates,
    select_head,
)


def test_fanout_capped_to_one_for_equiv_en():
    assert extract_edges(
        "a male given name, equivalent to English John or Jonathan", "ru"
    ) == [("EQUIV_EN", "en", "john")]


def test_fanout_capped_to_one_for_fem_equiv():
    assert extract_edges(
        "a male given name, feminine equivalent Christiane, Christina", "de"
    ) == [("FEM_EQUIV", "de", "christiane")]


def test_fanout_stays_at_three_for_diminutive():
    assert extract_edges(
        "a diminutive of the female given names Katherine, Kathryn "
        "or Cathleen",
        "en",
    ) == [
        ("DIMINUTIVE_OF", "en", "katherine"),
        ("DIMINUTIVE_OF", "en", "kathryn"),
        ("DIMINUTIVE_OF", "en", "cathleen"),
    ]


def test_plural_type_head_no_longer_swallows_the_target():
    assert extract_target_candidates("the female given names Katherine") == [
        "Katherine"
    ]


def test_language_adjective_before_the_type_head_is_stripped():
    # `English` IS an attested English given name, so the old extractor's
    # 'English' target resolved and made a real, wrong edge.
    assert extract_target_candidates(
        "the English male given name Arthur"
    ) == ["Arthur"]


def test_bare_type_phrase_yields_no_candidate():
    assert extract_target_candidates("a female given name") == []


def test_equiv_en_target_is_normalized_as_english():
    assert extract_edges(
        "a male given name, equivalent to English \u00c9tienne", "ru"
    ) == [("EQUIV_EN", "en", "\u00e9tienne")]


def test_cross_language_edges_never_merge_clusters():
    # The 270-node blob, in miniature: two same-language clusters bridged by
    # a shared English node. Containment must keep them apart.
    edges = [
        (1, 2, "DIMINUTIVE_OF", False),
        (3, 4, "DIMINUTIVE_OF", False),
        (2, 10, "EQUIV_EN", True),
        (4, 10, "EQUIV_EN", True),
    ]
    assert build_components(edges) == {2: [1, 2], 4: [3, 4]}


def test_cross_language_only_graph_has_no_components():
    assert build_components([(1, 2, "EQUIV_EN", True)]) == {}


def test_singletons_are_not_clusters():
    assert build_components([]) == {}


def test_head_is_the_canonical_in_degree_winner():
    edges = [
        (1, 3, "DIMINUTIVE_OF", False),
        (2, 3, "DIMINUTIVE_OF", False),
        (4, 3, "VARIANT_OF", False),
    ]
    assert select_head([1, 2, 3, 4], edges) == 3


def test_symmetric_equivalence_does_not_pick_a_canonical():
    assert select_head([6, 5], [(5, 6, "FEM_EQUIV", False)]) == 5


def test_cross_language_edges_do_not_vote_for_head():
    edges = [(1, 2, "DIMINUTIVE_OF", False), (3, 9, "EQUIV_EN", True)]
    assert select_head([1, 2], edges) == 2


def test_ceiling_gate():
    assert assert_cluster_ceiling({1: list(range(70))}, 65) == (False, 70)
    assert assert_cluster_ceiling({1: [1, 2, 3]}, 65) == (True, 3)

def test_green_card_graph_round_trip(db):
    from app.models.generated_name import Language
    from app.models.semantic import (
        EstablishedName,
        EstablishedNameCluster,
        EstablishedNameEdge,
        Lexeme,
        Sense,
        Source,
    )

    source = Source(name="test", source_type="test")
    language = Language(name="English", code="en", script="Latn")
    db.add_all([source, language])
    db.flush()
    lexeme = Lexeme(
        language_id=language.id, lemma="Katherine",
        normalized_lemma="katherine", part_of_speech="name",
        source_id=source.id, source_entry_id="e1", raw_entry={},
    )
    db.add(lexeme)
    db.flush()
    sense = Sense(
        lexeme_id=lexeme.id, source_id=source.id, source_locator="e1:0",
        sense_index=0, definition="a female given name",
    )
    db.add(sense)
    db.flush()

    head = EstablishedName(
        language_id=language.id, lemma="Katherine",
        normalized_lemma="katherine", name_type="given", gender="f",
        source_lexeme_id=lexeme.id, source_sense_id=sense.id,
        meaning_text="pure", meaning_channel="ETYM_QUOTED",
    )
    dim = EstablishedName(
        language_id=language.id, lemma="Cathy", normalized_lemma="cathy",
        name_type="given", gender="f", source_lexeme_id=lexeme.id,
        source_sense_id=sense.id,
    )
    db.add_all([head, dim])
    db.flush()

    db.add(EstablishedNameEdge(
        source_name_id=dim.id, target_name_id=head.id,
        relation_type="DIMINUTIVE_OF", is_cross_language=False,
    ))
    cluster = EstablishedNameCluster(
        name_type="given", size=2, head_name_id=head.id,
        is_cross_language_merged=False,
    )
    db.add(cluster)
    db.flush()
    head.cluster_id = cluster.id
    dim.cluster_id = cluster.id
    dim.meaning_text = "pure"
    dim.meaning_channel = "EQUIV_PROPAGATED"
    dim.meaning_source_name_id = head.id
    db.commit()

    loaded = db.get(EstablishedName, dim.id)
    assert loaded.meaning_source_name.lemma == "Katherine"
    assert loaded.cluster.head_name_id == head.id

def test_edges_only_sourced_from_canonical_lexeme(db):
    """
    Two Lexeme rows share (lang, normalized_lemma, name_type) -- Wiktionary's
    own etymology split, same shape as 'Alan' (Celtic given name / Hebrew
    given name, variant of Elon). Only the row's own source_lexeme_id may
    contribute edges; the non-canonical lexeme's trigger must be dropped,
    not silently merged in and bridging two unrelated identities.
    """
    from app.models.generated_name import Language
    from app.models.semantic import EstablishedName, Lexeme, Sense, Source
    from scripts.build_name_graph import collect_edges, load_name_index

    source = Source(name="test", source_type="test")
    language = Language(name="English", code="en", script="Latn")
    db.add_all([source, language])
    db.flush()

    canonical_lex = Lexeme(
        language_id=language.id, lemma="Alan", normalized_lemma="alan",
        part_of_speech="name", source_id=source.id,
        source_entry_id="e1", raw_entry={},
    )
    other_lex = Lexeme(
        language_id=language.id, lemma="Alan", normalized_lemma="alan",
        part_of_speech="name", source_id=source.id,
        source_entry_id="e2", raw_entry={},
    )
    target_lex = Lexeme(
        language_id=language.id, lemma="Elon", normalized_lemma="elon",
        part_of_speech="name", source_id=source.id,
        source_entry_id="e3", raw_entry={},
    )
    db.add_all([canonical_lex, other_lex, target_lex])
    db.flush()

    canonical_sense = Sense(
        lexeme_id=canonical_lex.id, source_id=source.id,
        source_locator="e1:0", sense_index=0,
        definition="a male given name from the Celtic languages",
    )
    # The non-canonical sense carries the ONLY trigger in this test --
    # if it leaks through, the edge below will exist and the test fails.
    other_sense = Sense(
        lexeme_id=other_lex.id, source_id=source.id,
        source_locator="e2:0", sense_index=0,
        definition="a male given name from Hebrew, variant of Elon",
    )
    target_sense = Sense(
        lexeme_id=target_lex.id, source_id=source.id,
        source_locator="e3:0", sense_index=0,
        definition="a male given name",
    )
    db.add_all([canonical_sense, other_sense, target_sense])
    db.flush()

    # established_names row's source_sense/source_lexeme point at the
    # CANONICAL (Celtic) lexeme -- Stage 3's deterministic choice.
    row = EstablishedName(
        language_id=language.id, lemma="Alan", normalized_lemma="alan",
        name_type="given", gender="m",
        source_lexeme_id=canonical_lex.id, source_sense_id=canonical_sense.id,
    )
    target_row = EstablishedName(
        language_id=language.id, lemma="Elon", normalized_lemma="elon",
        name_type="given", gender="m",
        source_lexeme_id=target_lex.id, source_sense_id=target_sense.id,
    )
    db.add_all([row, target_row])
    db.commit()

    index, meta, canonical_lexeme_of = load_name_index(db)
    edges, stats, _cross_type = collect_edges(db, index, meta, canonical_lexeme_of)

    assert edges == []
    assert stats["non_canonical_lexeme_skipped"] == 1

def test_ceiling_reflects_the_measured_alexander_family():
    # 83 real members (Albert/Alan/Alfred/Alexander/Alfonso/Abigail/Alice/
    # Alison/Alyssa share hub nicknames Al/Ally/Allie/Abbey/Abby, confirmed
    # via gloss inspection -- not a bug). Ceiling carries ~20% headroom,
    # same margin the original 65 used over its 54-node measurement.
    assert assert_cluster_ceiling({1: list(range(83))}, 100) == (True, 83)
    assert assert_cluster_ceiling({1: list(range(101))}, 100) == (False, 101)