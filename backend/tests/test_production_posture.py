"""
Stage 3c: the production app's posture, proven on a real FastAPI app built
by create_app(). Imports app.main (so torch loads) but never loads the model
or opens a database connection: prewarm is off and get_db is overridden.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.api.routes import senses as senses_route
from app.config import Settings
from app.db.session import get_db
from app.main import create_app
from app.services import root_llm


def _cfg(**overrides) -> Settings:
    base = dict(database_url="postgresql+psycopg://test@localhost/test",
                prewarm_on_startup=False, sentry_dsn=None)
    base.update(overrides)
    return Settings(_env_file=None, **base)  # type: ignore[call-arg]


@pytest.fixture(autouse=True)
def _restore_llm_fence(monkeypatch):
    # create_app(production) fences the trickle process-wide; undo per test.
    monkeypatch.setattr(root_llm, "_QUERY_TIME_LIVE", root_llm._QUERY_TIME_LIVE)


class _FakeDB:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail

    def execute(self, *args, **kwargs):
        if self.fail:
            raise RuntimeError("database unavailable")
        return None


def _client(cfg: Settings, db: _FakeDB | None = None) -> TestClient:
    app = create_app(cfg)
    fake = db or _FakeDB()
    app.dependency_overrides[get_db] = lambda: fake
    return TestClient(app)


def test_production_serves_no_api_docs():
    with _client(_cfg(app_env="production")) as client:
        assert client.get("/docs").status_code == 404
        assert client.get("/redoc").status_code == 404
        assert client.get("/openapi.json").status_code == 404
        assert client.get("/health").status_code == 200


def test_local_still_serves_api_docs():
    with _client(_cfg()) as client:
        assert client.get("/openapi.json").status_code == 200


@pytest.mark.parametrize(("app_env", "expected"), [("local", True), ("production", False)])
def test_include_hidden_is_ignored_in_production(monkeypatch, app_env, expected):
    seen: dict[str, bool] = {}

    def fake_lookup(db, *, query, language_code, include_hidden, limit):
        seen["include_hidden"] = include_hidden
        return []

    monkeypatch.setattr(senses_route, "lookup_sense_options", fake_lookup)
    with _client(_cfg(app_env=app_env)) as client:
        r = client.get("/senses/lookup", params={"query": "light", "includeHidden": "true"})
    assert r.status_code == 200
    assert seen["include_hidden"] is expected


def test_production_rate_limits_explore():
    # json=[] is a guaranteed 422: the route never runs, so no DB is touched.
    with _client(_cfg(app_env="production", rate_limit_explore_per_minute=2)) as client:
        codes = [client.post("/explore-v2", json=[]).status_code for _ in range(3)]
    assert codes == [422, 422, 429]


def test_local_does_not_rate_limit():
    with _client(_cfg()) as client:
        codes = [client.post("/explore-v2", json=[]).status_code for _ in range(30)]
    assert 429 not in codes


def test_production_fences_query_time_llm(monkeypatch):
    monkeypatch.setenv("ROOT_LLM_API_KEY", "test-key-not-real")
    monkeypatch.setattr(root_llm, "_QUERY_TIME_LIVE", True)
    create_app(_cfg(app_env="production"))
    assert root_llm.query_time_live() is False


def test_production_llm_opt_in_is_respected(monkeypatch):
    monkeypatch.setenv("ROOT_LLM_API_KEY", "test-key-not-real")
    monkeypatch.setattr(root_llm, "_QUERY_TIME_LIVE", True)
    create_app(_cfg(app_env="production", allow_query_time_llm_in_production=True))
    assert root_llm.query_time_live() is True


def test_ready_reflects_database_state():
    with _client(_cfg()) as client:
        assert client.get("/ready").status_code == 200
    with _client(_cfg(), db=_FakeDB(fail=True)) as client:
        assert client.get("/ready").status_code == 503
