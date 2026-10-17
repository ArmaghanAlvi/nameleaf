"""B5 (A-C.1): /ready reports a failed pre-warm as not ready, and a retry in
the background flips it to ready without a restart. The pre-warm itself is
faked; get_db is faked; nothing loads the model or touches Postgres."""
from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from app import main as app_main
from app import runtime_state
from app.config import Settings
from app.db.session import get_db


def _cfg(**overrides) -> Settings:
    base = dict(database_url="postgresql+psycopg://test@localhost/test",
                sentry_dsn=None)
    base.update(overrides)
    return Settings(_env_file=None, **base)  # type: ignore[call-arg]


class _FakeDB:
    def execute(self, *args, **kwargs):
        return None


def _client(cfg: Settings) -> TestClient:
    app = app_main.create_app(cfg)
    app.dependency_overrides[get_db] = lambda: _FakeDB()
    return TestClient(app)


@pytest.fixture
def fake_prewarm(monkeypatch):
    """Fails the first `fails` calls, then succeeds -- recording outcomes
    exactly as the real _prewarm does."""
    state = {"fails": 0, "calls": 0}

    def prewarm() -> bool:
        state["calls"] += 1
        ok = state["calls"] > state["fails"]
        runtime_state.mark_prewarm_finished(ok=ok)
        return ok

    monkeypatch.setattr(app_main, "_prewarm", prewarm)
    return state


def test_successful_prewarm_is_ready(fake_prewarm):
    with _client(_cfg(prewarm_on_startup=True)) as client:
        r = client.get("/ready")
    assert r.status_code == 200
    assert r.json()["prewarmOk"] is True


def test_failed_prewarm_is_not_ready_then_recovers(fake_prewarm, monkeypatch):
    fake_prewarm["fails"] = 1
    monkeypatch.setattr(app_main, "_PREWARM_RETRY_DELAYS", (0.4,))
    with _client(_cfg(prewarm_on_startup=True)) as client:
        first = client.get("/ready")
        assert first.status_code == 503
        assert first.json()["prewarmOk"] is False
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            r = client.get("/ready")
            if r.status_code == 200:
                break
            time.sleep(0.05)
    assert r.status_code == 200
    assert r.json()["prewarmOk"] is True
    assert fake_prewarm["calls"] == 2


def test_skipped_prewarm_stays_ready(fake_prewarm):
    with _client(_cfg(prewarm_on_startup=False)) as client:
        r = client.get("/ready")
    assert r.status_code == 200
    assert r.json()["prewarmOk"] is None
    assert fake_prewarm["calls"] == 0


def test_retry_stops_at_shutdown(fake_prewarm, monkeypatch):
    fake_prewarm["fails"] = 10**6           # never recovers
    monkeypatch.setattr(app_main, "_PREWARM_RETRY_DELAYS", (0.05,))
    with _client(_cfg(prewarm_on_startup=True)):
        time.sleep(0.3)
    calls = fake_prewarm["calls"]
    assert calls >= 2
    time.sleep(0.3)
    assert fake_prewarm["calls"] == calls    # no retries after shutdown


def test_prewarm_logs_device_and_threads(monkeypatch, caplog):
    """C-5.13: the real _prewarm reports the model's resolved device and the
    pre-warm thread's torch thread count on the uvicorn.error logger (the
    one uvicorn shows). Everything it imports is faked: no model, no DB."""
    import logging

    import torch

    import app.db.session as session_mod
    import app.services.embedding_provider as ep
    import app.services.language_directory as language_directory
    import app.services.parallel_expansion as px

    class _Model:
        device = torch.device("cpu")

    class _Session:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(ep, "get_model", lambda: _Model())
    monkeypatch.setattr(ep, "embed_query", lambda text: [0.0])
    monkeypatch.setattr(session_mod, "SessionLocal", lambda: _Session())
    monkeypatch.setattr(language_directory, "visible_languages", lambda db: [])
    monkeypatch.setattr(px, "_pivot_eligible_languages", lambda db: set())
    # mark_prewarm_finished mutates module globals; put them back afterwards.
    for name in ("_prewarm_finished", "_prewarm_ok", "_prewarm_attempts"):
        monkeypatch.setattr(runtime_state, name, getattr(runtime_state, name))

    with caplog.at_level(logging.INFO, logger="uvicorn.error"):
        assert app_main._prewarm() is True

    lines = [r.getMessage() for r in caplog.records
             if "startup pre-warm finished" in r.getMessage()]
    assert len(lines) == 1, caplog.text
    assert "embedding_device=cpu" in lines[0]
    assert f"prewarm_thread_torch_num_threads={torch.get_num_threads()}" in lines[0]
