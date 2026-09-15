"""Stage 3a/3g: settings derive safe production defaults; Sentry scrubbing."""
from __future__ import annotations

from app.config import Settings
from app.observability import scrub_event


def _cfg(**overrides) -> Settings:
    base = dict(database_url="postgresql+psycopg://test@localhost/test")
    base.update(overrides)
    return Settings(_env_file=None, **base)  # type: ignore[call-arg]


def test_local_defaults_match_pre_stage_behaviour():
    cfg = _cfg()
    assert cfg.docs_enabled and cfg.include_hidden_allowed
    assert not cfg.rate_limiting_on
    assert cfg.cors_origin_list == ["http://localhost:3000", "http://127.0.0.1:3000"]


def test_production_defaults_are_locked_down():
    cfg = _cfg(app_env="production")
    assert not cfg.docs_enabled
    assert not cfg.include_hidden_allowed
    assert cfg.rate_limiting_on


def test_explicit_override_beats_app_env():
    assert _cfg(app_env="production", expose_api_docs=True).docs_enabled
    assert not _cfg(app_env="local", rate_limit_enabled=False).rate_limiting_on


def test_scrub_removes_gemini_key_from_any_field():
    event = {"exception": {"values": [{"value":
        "Client error '429' for url 'https://generativelanguage.googleapis.com/"
        "v1beta/models/m:generateContent?key=AIzaSECRET123'"}]},
        "breadcrumbs": [{"data": {"url": "https://x/y?alt=json&key=AIzaSECRET123"}}]}
    scrubbed = scrub_event(event)
    assert scrubbed is not None
    text = str(scrubbed)
    assert "AIzaSECRET123" not in text
    assert "[scrubbed]" in text
