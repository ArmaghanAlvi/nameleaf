"""
Stage 2/3 unit tests. Pure functions plus one model smoke test; no Postgres.

Every case here is drawn from a real gloss or category string that appears in
the N1 census output, not invented -- a test built on a shape the corpus does
not contain proves nothing.
"""
from app.models.semantic import (
    EstablishedName,
    EstablishedNameCluster,
    EstablishedNameEdge,
    EstablishedNameToken,
)
from app.services.established_names import (
    category_names,
    classify_from_categories,
    classify_from_categories_origin,
    classify_sense,
    etymology_mentions,
    extract_equivalence,
    extract_meaning,
    gender_from_head,
    homograph_confidence,
    language_header_warning,
    meaning_tokens,
    parse_name_category,
    parse_name_category_origin,
    provenance_label,
    reduce_gender,
)


def cats(*names):
    """wiktextract shape: categories are DICTS, not strings."""
    return [{"name": n, "kind": "other", "parents": []} for n in names]


# --- category_names ---------------------------------------------------------

def test_category_names_reads_dicts():
    assert category_names(cats("German surnames")) == ["German surnames"]


def test_category_names_tolerates_strings_and_junk():
    assert category_names(["English surnames", {"kind": "other"}, None]) == [
        "English surnames"
    ]


def test_category_names_on_empty():
    assert category_names(None) == []


# --- parse_name_category ----------------------------------------------------

def test_category_gendered_given_name():
    assert parse_name_category("Old English male given names",
                               "Old English") == ("GIVEN", "m")


def test_category_genderless_surname():
    assert parse_name_category("German surnames", "German") == ("SURNAME", "u")


def test_category_patronymic():
    assert parse_name_category("Icelandic patronymics",
                               "Icelandic") == ("PATRONYMIC", "u")


def test_category_nomina_gentilia_is_a_surname():
    # Latin has no "Latin surnames" category at all; 1,131 name senses sit
    # here instead.
    assert parse_name_category("Latin nomina gentilia",
                               "Latin") == ("SURNAME", "u")


def test_category_allows_the_one_evidenced_modifier():
    assert parse_name_category("Russian possessive surnames",
                               "Russian") == ("SURNAME", "u")


def test_category_from_suffix_is_ignored():
    assert parse_name_category("English surnames from Old English",
                               "English") == ("SURNAME", "u")


def test_category_rejects_unknown_modifier_run():
    # The trap this predicate exists to avoid: ends in a type phrase, is not
    # a name category.
    assert parse_name_category("English terms derived from given names",
                               "English") is None


def test_category_rejects_other_languages_categories():
    assert parse_name_category("Polish female surnames", "German") is None


def test_category_rejects_non_name_categories():
    assert parse_name_category("Places in England", "English") is None


# --- 14c: origin capture ----------------------------------------------------

def test_the_from_tail_is_captured_not_discarded():
    hit = parse_name_category_origin(
        "English surnames from Old English", "English")
    assert hit is not None
    assert (hit.bucket, hit.gender) == ("SURNAME", "u")
    assert hit.origin_language == "Old English"
    assert hit.origin_shape == "from"


def test_the_connector_source_language_is_captured():
    # Findings 19.4: this is the ONLY origin signal that reaches `Nadiya`.
    hit = parse_name_category_origin(
        "English renderings of Ukrainian female given names", "English")
    assert hit is not None
    assert (hit.bucket, hit.gender) == ("GIVEN", "f")
    assert hit.origin_language == "Ukrainian"
    assert hit.origin_shape == "rendering"


def test_a_bare_category_states_no_origin():
    hit = parse_name_category_origin("English given names", "English")
    assert hit is not None
    assert hit.origin_language is None and hit.origin_shape is None


def test_a_lowercase_tail_is_not_a_language():
    # "of"/"in" do not denote an origin, and an uncapitalized run is not a
    # language name. Both fail CLOSED -- no origin rather than a wrong one.
    hit = parse_name_category_origin(
        "Russian possessive surnames from patronymics", "Russian")
    assert hit is not None
    assert (hit.bucket, hit.gender) == ("SURNAME", "u")
    assert hit.origin_language is None


def test_the_capture_did_not_change_which_categories_resolve():
    # The 14c invariant, at unit scale: every pre-14c assertion still holds
    # through the unchanged wrapper. The corpus-scale version is the
    # dry-run diff in Breakdown G Step 10.
    assert parse_name_category("English surnames from Old English",
                               "English") == ("SURNAME", "u")
    assert parse_name_category("English terms derived from given names",
                               "English") is None
    assert parse_name_category("Places in England", "English") is None


def test_origin_reduction_prefers_the_connector_shape():
    cats = [
        "{'name': 'English given names from Ukrainian', 'kind': 'other'}",
        "{'name': 'English renderings of Ukrainian female given names', "
        "'kind': 'other'}",
    ]
    bucket, gender, _also, origin, shape = classify_from_categories_origin(
        cats, "English")
    assert bucket == "GIVEN"
    assert (origin, shape) == ("Ukrainian", "rendering")


# --- 14c: the maintenance flag ----------------------------------------------

def test_the_language_header_warning_is_an_exact_string_test():
    yes = ["{'name': 'English entries with incorrect language header', "
           "'kind': 'other'}"]
    no = ["{'name': 'English given names', 'kind': 'other'}"]
    assert language_header_warning(yes, "English") is True
    assert language_header_warning(no, "English") is False
    # Language-scoped: a German flag is not an English one.
    assert language_header_warning(yes, "German") is False


# --- classify_from_categories ----------------------------------------------

def test_given_beats_surname_and_sets_the_flag():
    assert classify_from_categories(
        cats("English male given names", "English surnames"), "English"
    ) == ("GIVEN", "m", True)


def test_conflicting_genders_collapse_to_unisex():
    assert classify_from_categories(
        cats("English male given names", "English female given names"),
        "English",
    ) == ("GIVEN", "x", False)


def test_no_resolvable_category_returns_none_bucket():
    assert classify_from_categories(cats("Terms with quotations"),
                                    "English")[0] is None


# --- reduce_gender ----------------------------------------------------------

def test_unknown_never_outvotes_a_definite_reading():
    assert reduce_gender({"u", "m"}) == "m"


def test_male_and_female_together_is_unisex():
    assert reduce_gender({"m", "f"}) == "x"


def test_all_unknown_stays_unknown():
    assert reduce_gender({"u"}) == "u"
    assert reduce_gender(set()) == "u"


# --- gender_from_head -------------------------------------------------------

def test_head_reads_surname_gender():
    # Part A's gender_of cannot: \bname\b does not match inside "surname".
    assert gender_from_head("a male surname") == "m"


def test_head_reads_patronymic_gender():
    assert gender_from_head("a female patronymic") == "f"


def test_head_takes_the_first_phrase_only():
    assert gender_from_head(
        "a male given name, feminine equivalent Christiane"
    ) == "m"


def test_head_reads_an_explicit_pair_as_unisex():
    assert gender_from_head("A male or female given name.") == "x"
    assert gender_from_head("a common-gender surname") == "x"


def test_head_returns_unknown_for_a_bare_surname():
    assert gender_from_head("A surname.") == "u"


# --- classify_sense ---------------------------------------------------------

def test_form_of_wins_over_an_inherited_category():
    # Latin carries 313 form-of name senses and they inherit categories.
    assert classify_sense("accusative singular of Zmyrna", [],
                          cats("Latin female given names"), "Latin")[0] \
        == "FORM_OF"


def test_grammatical_gender_in_raw_tags_is_ignored():
    # The 593m/2,447f German surname bug: raw_tags carries NOUN gender.
    assert classify_sense("a surname", ["masculine", "neuter"],
                          cats("German surnames"), "German") \
        == ("SURNAME", "u", False)


def test_category_gender_beats_contradicting_raw_tags():
    assert classify_sense("a male surname", ["feminine"],
                          cats("Polish male surnames"), "Polish") \
        == ("SURNAME", "m", False)


def test_head_phrase_fills_a_genderless_category():
    # Keeps Icelandic's 36m/35f patronymic split alive.
    assert classify_sense("a female patronymic", ["masculine"],
                          cats("Icelandic patronymics"), "Icelandic") \
        == ("PATRONYMIC", "f", False)


def test_falls_back_to_the_gloss_when_no_category_resolves():
    assert classify_sense("a female given name", [],
                          cats("Terms with quotations"), "English") \
        == ("GIVEN", "f", False)


# --- meaning waterfall ------------------------------------------------------

def test_gloss_meaning_wins_the_waterfall():
    assert extract_meaning('a female given name, meaning "inspiration"',
                           'From somewhere ("other").', "cy") \
        == ("inspiration", "GLOSS_MEANING")


def test_dithematic_etymology_keeps_both_elements():
    assert extract_meaning(
        "a male given name",
        'From Old English ead ("wealth") + weard ("guardian").',
        "ang",
    ) == ("wealth + guardian", "ETYM_QUOTED")


def test_etym_quoted_stays_gated_off_for_japanese():
    assert extract_meaning("a female given name",
                           'Something "stem" of the verb.', "ja") \
        == (None, None)


def test_no_channel_returns_a_pair_of_nones():
    # The schema CHECK requires text and channel to be null together.
    assert extract_meaning("A surname.", "", "en") == (None, None)


# --- equivalence ------------------------------------------------------------

def test_equivalence_is_extracted_independently():
    gloss = 'a male given name, equivalent to English John'
    assert extract_equivalence(gloss) == "John"


def test_equivalence_does_not_consume_the_meaning_slot():
    gloss = "a male given name, equivalent to English Edward"
    etym = 'From Old English ead ("wealth") + weard ("guardian").'
    assert extract_equivalence(gloss) == "Edward"
    assert extract_meaning(gloss, etym, "ang")[1] == "ETYM_QUOTED"


def test_no_equivalence_returns_none():
    assert extract_equivalence("a male given name") is None


# --- tokens -----------------------------------------------------------------

def test_tokens_drop_stopwords_and_dedupe():
    assert meaning_tokens("wealth, fortune + guardian of the light") == [
        "wealth", "fortune", "guardian", "light"
    ]


def test_tokens_are_canonical_join_keys():
    # normalize_lemma is what Lexeme.normalized_lemma is built with, so the
    # join surface must use it too. Here it applies NFKC (the compatibility
    # ligature) and casefolds; a bare .lower() would leave a key that never
    # matches. Precomposed accents are deliberately PRESERVED for English --
    # tia/tía must not collide (see normalize_lemma's docstring).
    assert meaning_tokens("\ufb01refly") == ["firefly"]
    assert meaning_tokens("Bràvery") == ["bràvery"]


def test_tokens_on_a_blank_meaning():
    assert meaning_tokens(None) == []
    assert meaning_tokens("of the and") == []


# --- model smoke ------------------------------------------------------------

def test_green_card_tables_round_trip(db):
    from app.models.generated_name import Language
    from app.models.semantic import Lexeme, Sense, Source

    source = Source(name="test", source_type="test")
    language = Language(name="Icelandic", code="is", script="Latn")
    db.add_all([source, language])
    db.flush()

    lexeme = Lexeme(
        language_id=language.id, lemma="Thor", normalized_lemma="thor",
        part_of_speech="name", source_id=source.id, source_entry_id="e1",
        raw_entry={},
    )
    db.add(lexeme)
    db.flush()

    sense = Sense(
        lexeme_id=lexeme.id, source_id=source.id, source_locator="e1:0",
        sense_index=0, definition="a male given name",
    )
    db.add(sense)
    db.flush()

    cluster = EstablishedNameCluster(name_type="given", size=1)
    db.add(cluster)
    db.flush()

    name = EstablishedName(
        language_id=language.id, lemma="Thor", normalized_lemma="thor",
        name_type="given", gender="m", source_lexeme_id=lexeme.id,
        source_sense_id=sense.id, meaning_text="thunder",
        meaning_channel="ETYM_QUOTED", cluster_id=cluster.id,
    )
    db.add(name)
    db.flush()

    other = EstablishedName(
        language_id=language.id, lemma="Þórr", normalized_lemma="þórr",
        name_type="given", gender="m", source_lexeme_id=lexeme.id,
        source_sense_id=sense.id,
    )
    db.add(other)
    db.flush()

    db.add(EstablishedNameToken(established_name_id=name.id, token="thunder"))
    db.add(EstablishedNameEdge(
        source_name_id=name.id, target_name_id=other.id,
        relation_type="VARIANT_OF", is_cross_language=False,
    ))
    cluster.head_name_id = name.id
    db.commit()

    loaded = db.get(EstablishedName, name.id)
    assert loaded.gender == "m"
    assert loaded.is_also_surname is False
    assert [t.token for t in loaded.tokens] == ["thunder"]
    assert loaded.cluster.head_name_id == name.id


def test_category_rendering_of_another_language():
    assert parse_name_category(
        "Chinese renderings of English surnames", "Chinese"
    ) == ("SURNAME", "u")


def test_category_rendering_with_gender():
    assert parse_name_category(
        "Korean renderings of English female given names", "Korean"
    ) == ("GIVEN", "f")


def test_category_rendering_of_multiword_source_language():
    assert parse_name_category(
        "Greek renderings of Ancient Greek surnames", "Greek"
    ) == ("SURNAME", "u")


def test_category_diminutive_connector():
    assert parse_name_category(
        "Icelandic diminutives of male given names", "Icelandic"
    ) == ("GIVEN", "m")


def test_category_nested_rendering_and_diminutive():
    # Real Wiktionary shape (Greek): the connector discards everything
    # before the trailing gender/type, so nesting is harmless.
    assert parse_name_category(
        "Greek renderings of English diminutives of male given names",
        "Greek",
    ) == ("GIVEN", "m")


def test_category_connector_without_a_recognized_tail_fails_closed():
    assert parse_name_category(
        "English renderings of the Cyrillic alphabet", "English"
    ) is None


# --- Stage 6: homograph honesty -------------------------------------------

def test_etymology_mention_latin():
    assert etymology_mentions('From Latin aur\u014dra ("dawn").', "aurora", "la")


def test_etymology_mention_devanagari_survives_the_matra():
    # TOKEN_RX would return आक + श here; the separator splitter must not.
    assert etymology_mentions(
        'From \u0906\u0915\u093e\u0936 (\u0101k\u0101\u015b, "sky").',
        "\u0906\u0915\u093e\u0936",
        "hi",
    )


def test_etymology_mention_arabic_survives_the_harakat():
    assert etymology_mentions(
        'From Arabic \u0646\u064f\u0648\u0631 (n\u016br, "light").',
        "\u0646\u0648\u0631",
        "ar",
    )


def test_lucius_the_pike_is_not_corroborated():
    # IMPORT_PREP_FINDINGS.md 5.1's worked counter-example.
    assert not etymology_mentions('From lux, lucis ("light").', "lucius", "la")


def test_homograph_confidence_levels():
    assert homograph_confidence(
        'From Latin aur\u014dra ("dawn").', "aurora", "la"
    ) == "corroborated"
    assert homograph_confidence(None, "lucius", "la") == "spelling_only"


def test_label_hedges_an_uncorroborated_homograph():
    label = provenance_label(
        meaning_channel="HOMOGRAPH",
        language_name="Latin",
        homograph_lemma="lucius",
        homograph_confidence_level="spelling_only",
    )
    assert "Spelled identically" in label
    assert "means" not in label


def test_label_asserts_a_corroborated_homograph():
    assert "own etymology" in provenance_label(
        meaning_channel="HOMOGRAPH",
        language_name="Latin",
        homograph_lemma="aurora",
        homograph_confidence_level="corroborated",
    )


def test_label_for_propagated_equivalence():
    assert "John" in provenance_label(
        meaning_channel="EQUIV_PROPAGATED",
        language_name="Russian",
        equiv_en_target="John",
    )


def test_label_for_true_residue():
    assert provenance_label(
        meaning_channel=None, language_name="Icelandic"
    ) == "Meaning not recorded"