"""B5: limits and admission through the real /explore-v2 route.

The app is built by create_app(); get_db is overridden onto a file-backed
SQLite engine in tmp_path, so nothing here can reach Postgres. A FILE, not
:memory: + StaticPool: concurrent searches commit from different threads, and
one shared sqlite3 connection used by two threads at once raises "bad
parameter or other API misuse" -- a fixture artifact that Postgres's
per-session pooled connections don't have. The SEARCH itself is replaced by a
controllable blocking fake: admission is tested without running the engine.
"""
from __future__ import annotations

import itertools
import threading
import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

import app.models  # noqa: F401
from app.api.routes import explore_v2 as route
from app.config import Settings
from app.db.base import Base
from app.db.session import get_db
from app.main import create_app
from app.models.semantic import SenseSelectionEvent, SenseSelectionStat
from app.schemas.explore_v2 import ExploreV2Response
from app.services import root_llm
from app.services.parallel_expansion import ParallelExpansion


def _cfg(**overrides) -> Settings:
    base = dict(database_url="postgresql+psycopg://test@localhost/test",
                prewarm_on_startup=False, sentry_dsn=None)
    base.update(overrides)
    return Settings(_env_file=None, **base)  # type: ignore[call-arg]


ON = dict(search_limits_enabled=True, search_admission_enabled=True)


@pytest.fixture
def engine(tmp_path):
    base = create_engine(f"sqlite+pysqlite:///{tmp_path / 'admission.db'}",
                         connect_args={"check_same_thread": False})
    # C2: schema "live" -> SQLite's default schema, for DDL and queries. The
    # worker session's get_bind() returns this same engine, so it inherits it.
    eng = base.execution_options(schema_translate_map={"live": None})
    Base.metadata.create_all(eng)
    yield eng
    base.dispose()


class Harness:
    """The app, the sessions it hands out, and a blocking fake search."""

    def __init__(self, engine, cfg: Settings, monkeypatch) -> None:
        self.engine = engine
        self.app = create_app(cfg)
        self.request_sessions: list[Session] = []
        self.search_sessions: list[Session] = []
        self.search_threads: list[str] = []
        self.record_flags: list[bool] = []
        self.release = threading.Event()
        self.block_when = lambda req: True
        self._lock = threading.Lock()
        self._ids = itertools.count(1)

        def override():
            db = Session(bind=engine, autoflush=False)
            with self._lock:
                self.request_sessions.append(db)
            try:
                yield db
            finally:
                db.close()

        self.app.dependency_overrides[get_db] = override

        def fake_search(db, request, commit=None, *, record):
            with self._lock:
                self.search_sessions.append(db)
                self.record_flags.append(record)
                self.search_threads.append(threading.current_thread().name)
                row_id = next(self._ids)
            if self.block_when(request):
                self.release.wait(10)
            # A write that must only land if the search is NOT abandoned.
            db.add(SenseSelectionStat(sense_id=row_id, selection_count=1))
            (commit or db.commit)()
            return ExploreV2Response(selectedSenseIds=request.selectedSenseIds,
                                     expandedSenses=[], results=[])

        monkeypatch.setattr(route, "_run_search", fake_search)
        self.client = TestClient(self.app)

    @property
    def admission(self):
        return self.app.state.search_policy.admission

    def post(self, width=1, depth=1, sense=1, **extra):
        body = {"selectedSenseIds": [sense], "width": width, "depth": depth,
                "languageCodes": ["en"], **extra}
        if width is None:
            body.pop("width")
        return self.client.post("/explore-v2", json=body)

    def stat_rows(self) -> int:
        with Session(bind=self.engine) as s:
            return s.scalar(select(func.count()).select_from(SenseSelectionStat))


def _wait_until(cond, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


def _bg(fn, out: list, *args, **kw):
    th = threading.Thread(target=lambda: out.append(fn(*args, **kw)))
    th.start()
    return th


# -- limits -------------------------------------------------------------------

@pytest.mark.parametrize("body", [
    {"selectedSenseIds": [1], "width": 4, "depth": 1, "languageCodes": ["en"]},
    {"selectedSenseIds": [1], "width": 1, "depth": 3, "languageCodes": ["en"],
     "_maxdepth": 2},
    # Legacy path: no width, expansionCount defaults to 10.
    {"selectedSenseIds": [1], "depth": 1},
    {"selectedSenseIds": [1], "expansionCount": 50, "depth": 1},
])
def test_over_limit_is_422_and_writes_nothing(engine, monkeypatch, body):
    body = dict(body)
    max_depth = body.pop("_maxdepth", 3)
    spy: list = []
    monkeypatch.setattr(route, "record_sense_selection",
                        lambda *a, **k: spy.append(a))
    app = create_app(_cfg(**ON, search_max_depth=max_depth))
    app.dependency_overrides[get_db] = lambda: Session(bind=engine)
    with TestClient(app) as client:
        r = client.post("/explore-v2", json=body)
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "search_limit_exceeded"
    assert isinstance(r.json()["detail"]["message"], str)
    assert spy == []
    with Session(bind=engine) as s:
        assert s.scalar(select(func.count()).select_from(SenseSelectionStat)) == 0


def test_at_limit_is_admitted(engine, monkeypatch):
    h = Harness(engine, _cfg(**ON), monkeypatch)
    h.block_when = lambda req: False
    with h.client:
        assert h.post(width=3, depth=3).status_code == 200


def test_settings_unset_means_no_limits_and_no_admission(engine, monkeypatch):
    h = Harness(engine, _cfg(), monkeypatch)
    h.block_when = lambda req: False
    with h.client:
        assert h.admission is None
        assert h.post(width=10, depth=3).status_code == 200
        assert h.post(width=None, depth=3).status_code == 200   # legacy, 10
    # Inline, in the REQUEST's own session, on the request's thread.
    assert h.search_sessions == h.request_sessions
    assert all(name != "search-worker" for name in h.search_threads)


# -- admission ----------------------------------------------------------------

def test_two_run_and_the_third_queues(engine, monkeypatch):
    h = Harness(engine, _cfg(**ON, search_queue_wait_seconds=5), monkeypatch)
    out: list = []
    with h.client:
        threads = [_bg(h.post, out) for _ in range(3)]
        assert _wait_until(lambda: len(h.search_sessions) == 2)
        assert _wait_until(lambda: h.admission.waiting == 1)
        assert h.admission.running == 2
        h.release.set()
        for th in threads:
            th.join(5)
    assert [r.status_code for r in out] == [200, 200, 200]
    assert len(h.search_sessions) == 3


def test_full_queue_is_an_immediate_503(engine, monkeypatch):
    h = Harness(engine, _cfg(**ON, search_queue_size=1,
                             search_queue_wait_seconds=5), monkeypatch)
    out: list = []
    with h.client:
        threads = [_bg(h.post, out) for _ in range(3)]
        assert _wait_until(lambda: h.admission.waiting == 1)
        r = h.post()
        h.release.set()
        for th in threads:
            th.join(5)
    assert r.status_code == 503
    assert r.json()["detail"]["code"] == "search_queue_full"
    assert r.headers["Retry-After"] == "5"
    assert r.json()["detail"]["retryAfter"] == 5


def test_queue_wait_over_limit_is_503_with_retry_after(engine, monkeypatch):
    h = Harness(engine, _cfg(**ON, search_queue_wait_seconds=0.3), monkeypatch)
    out: list = []
    with h.client:
        threads = [_bg(h.post, out) for _ in range(2)]
        assert _wait_until(lambda: h.admission.running == 2)
        r = h.post()
        h.release.set()
        for th in threads:
            th.join(5)
    assert r.status_code == 503
    assert r.json()["detail"]["code"] == "search_queue_timeout"
    assert r.headers["Retry-After"] == "1"


def test_second_large_is_503_while_a_normal_search_proceeds(engine, monkeypatch):
    h = Harness(engine, _cfg(**ON), monkeypatch)
    h.block_when = lambda req: req.width == 3       # only the large one blocks
    out: list = []
    with h.client:
        th = _bg(h.post, out, width=3, depth=3)
        assert _wait_until(lambda: h.admission.large_running == 1
                           and len(h.search_sessions) == 1)
        second_large = h.post(width=2, depth=2)
        normal = h.post(width=1, depth=3)
        h.release.set()
        th.join(5)
    assert second_large.status_code == 503
    assert second_large.json()["detail"]["code"] == "large_search_busy"
    assert second_large.headers["Retry-After"] == "120"
    assert normal.status_code == 200
    assert out[0].status_code == 200


def test_timeout_is_504_and_the_slot_stays_held(engine, monkeypatch):
    h = Harness(engine, _cfg(**ON, search_timeout_seconds=0.3), monkeypatch)
    with h.client:
        r = h.post()
        assert r.status_code == 504
        assert r.json()["detail"]["code"] == "search_timeout"
        # The search is still running: its slot must still be taken.
        assert h.admission.running == 1
        h.release.set()
        assert _wait_until(lambda: h.admission.running == 0)
    assert h.stat_rows() == 0          # abandoned -> nothing committed


def test_admitted_search_uses_its_own_session(engine, monkeypatch):
    h = Harness(engine, _cfg(**ON), monkeypatch)
    h.block_when = lambda req: False
    with h.client:
        assert h.post().status_code == 200
    assert len(h.search_sessions) == 1 and len(h.request_sessions) == 1
    assert h.search_sessions[0] is not h.request_sessions[0]
    assert h.search_sessions[0].get_bind() is engine
    assert h.search_threads == ["search-worker"]
    assert h.stat_rows() == 1          # a finished search does commit


def test_search_limits_endpoint_reports_settings(engine, monkeypatch):
    with TestClient(create_app(_cfg(**ON, search_max_width=5))) as client:
        body = client.get("/search-limits").json()
    assert body == {"maxWidth": 5, "maxDepth": 3, "largeThreshold": 4,
                    "limitsEnforced": True, "admissionEnforced": True}
    with TestClient(create_app(_cfg())) as client:
        body = client.get("/search-limits").json()
    assert body["limitsEnforced"] is False and body["maxWidth"] == 3


# -- RANKING_STATS_WRITE (C2) --------------------------------------------------

def _stub_engine(monkeypatch):
    """Run the REAL _run_search -- including its record decision and its
    commit -- with only the engine calls stubbed to empty results."""
    monkeypatch.setattr(route, "parallel_expand", lambda *a, **k:
                        ParallelExpansion(trees={}, interleaved=[]))
    monkeypatch.setattr(route, "retrieve_green_cards", lambda *a, **k: [])
    monkeypatch.setattr(route, "build_views", lambda *a, **k: [])


def _search_and_count(engine, cfg: Settings) -> tuple[int, int]:
    app = create_app(cfg)

    def override():
        db = Session(bind=engine, autoflush=False)
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override
    with TestClient(app) as client:
        r = client.post("/explore-v2", json={
            "selectedSenseIds": [42], "width": 1, "depth": 1,
            "languageCodes": ["en"]})
    assert r.status_code == 200, r.text
    with Session(bind=engine) as s:
        return (s.scalar(select(func.count()).select_from(SenseSelectionStat)),
                s.scalar(select(func.count()).select_from(SenseSelectionEvent)))


@pytest.fixture
def _restore_llm_fence(monkeypatch):
    # create_app(production) fences the trickle process-wide; undo per test.
    monkeypatch.setattr(root_llm, "_QUERY_TIME_LIVE", root_llm._QUERY_TIME_LIVE)


def test_production_search_writes_no_statistics(engine, monkeypatch,
                                                _restore_llm_fence):
    _stub_engine(monkeypatch)
    assert _search_and_count(engine, _cfg(app_env="production")) == (0, 0)


def test_local_default_still_records_statistics(engine, monkeypatch):
    _stub_engine(monkeypatch)
    assert _search_and_count(engine, _cfg()) == (1, 1)


def test_explicit_switch_wins_over_app_env(engine, monkeypatch,
                                           _restore_llm_fence):
    _stub_engine(monkeypatch)
    assert _search_and_count(engine, _cfg(ranking_stats_write=False)) == (0, 0)


def test_route_passes_the_stats_switch_explicitly(engine, monkeypatch):
    h = Harness(engine, _cfg(ranking_stats_write=False), monkeypatch)
    h.block_when = lambda req: False
    with h.client:
        assert h.post().status_code == 200
    assert h.record_flags == [False]
