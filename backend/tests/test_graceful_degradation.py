"""
Stage 3d: failures that must never take the site down.

The live-LLM database path can't run on SQLite (it uses pg_insert), so what
is tested here is the two guarantees production actually leans on: a failed
pre-warm doesn't block startup, and a missing key keeps the trickle off.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from app import runtime_state
from app.config import Settings
from app.main import create_app
from app.services import embedding_provider, root_llm


def _cfg(**overrides) -> Settings:
    base = dict(database_url="postgresql+psycopg://test@localhost/test", sentry_dsn=None)
    base.update(overrides)
    return Settings(_env_file=None, **base)  # type: ignore[call-arg]


def test_prewarm_failure_does_not_block_startup(monkeypatch):
    def boom():
        raise RuntimeError("model unavailable")

    monkeypatch.setattr(embedding_provider, "get_model", boom)
    with TestClient(create_app(_cfg(prewarm_on_startup=True))) as client:
        assert client.get("/health").status_code == 200
    assert runtime_state.prewarm_status() == (True, False)


def test_missing_key_keeps_trickle_off(monkeypatch):
    monkeypatch.delenv("ROOT_LLM_API_KEY", raising=False)
    monkeypatch.setattr(root_llm, "_QUERY_TIME_LIVE", True)
    assert root_llm.query_time_live() is False
