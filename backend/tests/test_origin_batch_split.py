"""The split-on-400 fallback. NO NETWORK -- run_batch is monkeypatched."""
import httpx
import pytest

import scripts.run_name_origin_pass as runner


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