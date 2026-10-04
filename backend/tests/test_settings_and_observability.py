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


def test_search_limits_and_admission_derive_from_app_env():
    local, prod = _cfg(), _cfg(app_env="production")
    assert not local.search_limits_on and not local.search_admission_on
    assert prod.search_limits_on and prod.search_admission_on
    # The numbers ARE the production values.
    assert (prod.search_max_width, prod.search_max_depth) == (3, 3)
    assert (prod.search_large_threshold, prod.search_concurrency,
            prod.search_large_concurrency) == (4, 2, 1)
    assert (prod.search_queue_size, prod.search_queue_wait_seconds,
            prod.search_timeout_seconds) == (4, 30.0, 300.0)


def test_search_switches_override_app_env():
    assert not _cfg(app_env="production",
                    search_limits_enabled=False).search_limits_on
    assert _cfg(search_admission_enabled=True).search_admission_on


def test_large_lane_must_leave_a_normal_slot():
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        _cfg(app_env="production", search_large_concurrency=2,
             search_concurrency=2)
    # Only enforced when admission is on.
    _cfg(search_large_concurrency=2, search_concurrency=2)


def test_ranking_stats_write_derives_from_app_env():
    # C2: statistics are recorded locally, never in production by default.
    assert _cfg().ranking_stats_write_on
    assert not _cfg(app_env="production").ranking_stats_write_on


def test_ranking_stats_write_explicit_override_wins():
    assert _cfg(app_env="production",
                ranking_stats_write=True).ranking_stats_write_on
    assert not _cfg(ranking_stats_write=False).ranking_stats_write_on
