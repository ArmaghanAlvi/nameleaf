"""The split-on-400 fallback. NO NETWORK -- run_batch is monkeypatched."""
import httpx
import pytest

import scripts.run_name_origin_pass as runner

from app.models.semantic import EstablishedName
from app.services import name_origin as assembly

class Row:
    def __init__(self, nid):
        self.established_name_id = nid
        self.name_type = "surname"
        self.normalized_lemma = f"r{nid}"
        self.source_sense_id = None
        self.twin_languages = ()


def _http_400():
    request = httpx.Request("POST", "https://example.invalid")
    response = httpx.Response(400, request=request, text="INVALID_ARGUMENT")
    return httpx.HTTPStatusError("400", request=request, response=response)


def test_splits_on_400_and_covers_every_row(monkeypatch):
    calls = []

    def fake(name_type, rows, vocab, host, *, shuffle_seed=None):
        calls.append(len(rows))
        if len(rows) > 10:
            raise _http_400()
        return {r.established_name_id: object() for r in rows}, "m", {}

    monkeypatch.setattr(runner, "run_batch", fake)
    rows = [Row(i) for i in range(40)]
    splits: list[int] = []
    chunks, spent = runner.ab_pair("surname", rows, [], "English",
                                   splits=splits)
    covered = [r.established_name_id
               for chunk, _, _, _, _ in chunks for r in chunk]
    assert sorted(covered) == list(range(40))
    assert splits == [40, 20, 20]
    assert spent > 0


def test_non_400_is_not_split(monkeypatch):
    def fake(*a, **k):
        raise RuntimeError("transport")

    monkeypatch.setattr(runner, "run_batch", fake)
    with pytest.raises(RuntimeError):
        runner.ab_pair("surname", [Row(i) for i in range(40)], [],
                       "English", splits=[])


def test_min_size_stops_the_recursion(monkeypatch):
    def fake(*a, **k):
        raise _http_400()

    monkeypatch.setattr(runner, "run_batch", fake)
    with pytest.raises(httpx.HTTPStatusError):
        runner.ab_pair("surname", [Row(i) for i in range(4)], [],
                       "English", splits=[], min_size=5)


def _seed_error_row(db, *, attempt_count: int):
    """One established_names row already marked llm_error by apply_origin,
    with a matching name_origin_attempts row at the given attempt_count.

    EstablishedName.source_lexeme_id and .source_sense_id are BOTH
    Mapped[int] (non-nullable) -- unlike NameOriginAttempt.source_sense_id,
    which is nullable. So a real Lexeme and Sense are required here, not
    optional scaffolding. Mirrors test_green_card_tables_round_trip's setup
    exactly for that reason, then skips only what select_errors's query
    never reads: cluster, tokens, edges.

    HOST_CODE ('en') is required: select_errors joins on l.code = :code
    with HOST_CODE bound from assembly.HOST_CODE, not a parameter, so the
    seeded language's code must match it exactly."""
    from app.models.generated_name import Language
    from app.models.semantic import Lexeme, NameOriginAttempt, Sense, Source

    language = Language(name="English", code=assembly.HOST_CODE,
                        script="Latn")
    source = Source(name="test", source_type="test")
    db.add_all([language, source])
    db.flush()

    lexeme = Lexeme(
        language_id=language.id, lemma="Thorald", normalized_lemma="thorald",
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

    name = EstablishedName(
        language_id=language.id, lemma="Thorald", normalized_lemma="thorald",
        name_type="given", gender="m", source_lexeme_id=lexeme.id,
        source_sense_id=sense.id, meaning_text="thunder ruler",
        meaning_channel="ETYM_QUOTED", origin_source="llm_error",
    )
    db.add(name)
    db.flush()

    db.add(NameOriginAttempt(
        language_id=language.id, normalized_lemma="thorald",
        name_type="given", status="error", attempt_count=attempt_count,
        model="test",
    ))
    db.commit()
    return name


def test_select_errors_finds_rows_apply_origin_has_already_marked(db):
    """The structural point: once a pass completes and is applied,
    origin_source is non-NULL on every row and select_pending returns
    nothing. select_errors must still see the error backlog."""
    _seed_error_row(db, attempt_count=1)
    assert assembly.select_pending(db) == []
    items = assembly.select_errors(db)
    assert len(items) == 1
    assert items[0].meaning_text is not None   # full payload, not thin


def test_select_errors_respects_the_attempt_ceiling(db):
    _seed_error_row(db, attempt_count=3)
    assert assembly.select_errors(db, max_attempts=3) == []