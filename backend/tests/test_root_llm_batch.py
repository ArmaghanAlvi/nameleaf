"""
Stage 16b. First tests to touch root_llm.py at all.

SCOPE, and why it is this narrow: resolve_llm_roots writes through
pg_insert, which the SQLite conftest cannot execute, so the DB path is not
unit-testable here. What IS testable is every pure function between the
model's bytes and the ledger's grain -- prompt assembly, schema shape, and
the parser's refusal to invent a key. The ledger-grain contract itself is
verified by SQL reconciliation in Step 4, deliberately and not by pretending
pytest covers it.
"""
import json
import pytest
from typing import cast
from sqlalchemy.orm import Session

from app.services import root_llm as rl


def test_batch_schema_requires_every_code():
    schema = rl._batch_schema(["ru", "ja", "ar"])
    assert schema["type"] == "OBJECT"
    assert set(schema["properties"]) == {"ru", "ja", "ar"}
    assert schema["required"] == ["ru", "ja", "ar"]
    assert schema["properties"]["ru"]["items"]["type"] == "STRING"


def test_batch_schema_rejects_an_unsafe_code():
    with pytest.raises(ValueError):
        rl._batch_schema(["ru", "zh-hant"])


def test_parse_keeps_only_requested_codes():
    parsed = json.loads('{"ru": ["\\u0441\\u0432\\u0435\\u0442"], '
                        '"fr": ["lumiere"]}')
    out = rl._parse_batch_payload(parsed, ["ru", "ja"])
    assert set(out) == {"ru"}          # fr was never asked for
    assert "ja" not in out             # and ja is NOT invented as []


def test_parse_trims_blanks_and_caps_at_three():
    out = rl._parse_batch_payload(
        {"la": [" lux ", "", "lumen", "iubar", "fax"]}, ["la"])
    assert out["la"] == ["lux", "lumen", "iubar"]


def test_parse_drops_a_non_list_value():
    out = rl._parse_batch_payload({"la": "lux", "ru": ["свет"]},
                                  ["la", "ru"])
    assert out == {"ru": ["свет"]}


def test_parse_rejects_a_non_object_response():
    with pytest.raises(ValueError):
        rl._parse_batch_payload(["lux"], ["la"])


def test_batch_prompt_lists_every_language():
    prompt = rl._BATCH_PROMPT.format(
        lemma="light", pos="noun", gloss="electromagnetic radiation",
        language_list="  ru = Russian\n  ja = Japanese")
    assert "ru = Russian" in prompt
    assert "ja = Japanese" in prompt
    assert "light (noun)" in prompt


def test_single_wrapper_delegates_to_the_batched_path(monkeypatch):
    """The wrapper must not grow a second implementation. If someone
    reintroduces per-pair logic here, the two paths drift and only
    production notices."""
    seen = {}

    def fake(db, *, english_sense_id, language_codes):
        seen["args"] = (english_sense_id, language_codes)
        return {"ru": 4242}

    monkeypatch.setattr(rl, "resolve_llm_roots", fake)
    got = rl.resolve_llm_root(cast(Session, None), english_sense_id=7,
                              language_code="ru")
    assert got == 4242
    assert seen["args"] == (7, ["ru"])