"""
Stage 19c/19d. What the origin pass is allowed to ask about, and in what
order.

These are contract tests, not coverage. The two things that would be
expensive to discover later are (a) the pass asking about rows nobody sees
before the ones they do, and (b) language_header_warning leaking into the
payload -- §20.6 measured it at 86.4% corpus-wide boilerplate, so sending it
biases the model toward "foreign" on natively-filed rows.
"""
import pytest

from app.models.generated_name import Language
from app.models.semantic import (
    EstablishedName, Lexeme, NameOriginTwin, Sense, Source,
)
from app.services.name_origin import (
    item_payload, origin_vocabulary, select_pending,
)


@pytest.fixture
def corpus(db):
    en = Language(code="en", name="English")
    ar = Language(code="ar", name="Arabic")
    db.add_all([en, ar])
    db.flush()

    src = Source(slug="test-src", name="Test Source",
                source_type="dictionary_dump")
    db.add(src)
    db.flush()

    lex = Lexeme(language_id=en.id, lemma="X", normalized_lemma="x",
                 part_of_speech="name", source_id=src.id,
                 source_entry_id="test-entry-1")
    db.add(lex)
    db.flush()
    sense = Sense(lexeme_id=lex.id, source_id=src.id,
                  source_locator="test-locator-1", sense_index=0,
                  definition="d", visibility_status="hidden")
    db.add(sense)
    db.flush()

    def _name(lemma, norm, ntype, *, lang, meaning=None, channel=None,
              warn=False, gender="u", source=None):
        row = EstablishedName(
            language_id=lang.id, lemma=lemma, normalized_lemma=norm,
            name_type=ntype, gender=gender, is_also_surname=False,
            source_lexeme_id=lex.id, source_sense_id=sense.id,
            meaning_text=meaning, meaning_channel=channel,
            language_header_warning=warn, origin_source=source,
        )
        db.add(row)
        return row

    _name("Zeta", "zeta", "given", lang=en)                       # no meaning
    _name("Amal", "amal", "given", lang=en, meaning="hope",
          channel="GLOSS_MEANING", warn=True, gender="f")         # meaning
    _name("Done", "done", "surname", lang=en, source="category")  # not pending
    _name("أمل", "امل", "given", lang=ar)
    db.add(NameOriginTwin(
        language_id=en.id, normalized_lemma="amal", name_type="given",
        twin_language_id=ar.id, twin_lemma="أمل", match_key="amal"))
    db.commit()
    return db


def test_rows_with_a_meaning_are_asked_about_first(corpus):
    items = select_pending(corpus)
    assert [i.lemma for i in items] == ["Amal", "Zeta"]


def test_rows_that_already_have_a_provenance_are_not_pending(corpus):
    assert "Done" not in {i.lemma for i in select_pending(corpus)}


def test_twin_evidence_is_attached(corpus):
    amal = next(i for i in select_pending(corpus) if i.lemma == "Amal")
    assert amal.twin_languages == ("Arabic",)


def test_language_header_warning_never_reaches_the_payload(corpus):
    """§20.6: 86.4% corpus-wide boilerplate. Amal carries the flag; the
    payload must not."""
    amal = next(i for i in select_pending(corpus) if i.lemma == "Amal")
    payload = item_payload(amal)
    blob = repr(payload).lower()
    assert "header" not in blob and "warning" not in blob
    assert payload["also_attested_in"] == ["Arabic"]
    assert payload["meaning"] == "hope"
    assert payload["gender"] == "f"


def test_default_gender_is_omitted_rather_than_asserted(corpus):
    zeta = next(i for i in select_pending(corpus) if i.lemma == "Zeta")
    assert "gender" not in item_payload(zeta)


def test_vocabulary_is_derived_from_the_languages_table(corpus):
    vocab = origin_vocabulary(corpus)
    assert vocab[-1] == "other"
    assert {"English", "Arabic"} <= set(vocab)