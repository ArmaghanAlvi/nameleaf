"""B5: the admission controller and the keep-the-slot search job, unit-level.

Threads and short timings; no app, no database beyond an in-memory SQLite
engine for the commit/abandon race.
"""
from __future__ import annotations

import threading
import time

import pytest
from fastapi import HTTPException
from sqlalchemy import Column, Integer, MetaData, Table, create_engine, func, select
from sqlalchemy.pool import StaticPool

from app.search_admission import AdmissionController, SearchPolicy, _SearchJob


def _controller(**kw) -> AdmissionController:
    base = dict(slots=2, large_slots=1, queue_size=4, queue_wait_seconds=2.0,
                timeout_seconds=5.0)
    base.update(kw)
    return AdmissionController(**base)


def _wait_until(cond, timeout=2.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


def _code(exc: HTTPException) -> str:
    return exc.detail["code"]  # type: ignore[index]


def test_two_run_third_queues_then_proceeds():
    ac = _controller()
    t1, t2 = ac.acquire(False), ac.acquire(False)
    got: list = []
    th = threading.Thread(target=lambda: got.append(ac.acquire(False)))
    th.start()
    assert _wait_until(lambda: ac.waiting == 1)
    assert ac.running == 2 and not got
    ac.release(t1)
    th.join(2)
    assert got and ac.running == 2 and ac.waiting == 0
    ac.release(t2)
    ac.release(got[0])
    assert ac.running == 0


def test_full_queue_is_an_immediate_503():
    ac = _controller(queue_size=1)
    held = [ac.acquire(False), ac.acquire(False)]
    th = threading.Thread(target=lambda: _swallow(ac.acquire, False))
    th.start()
    assert _wait_until(lambda: ac.waiting == 1)
    t0 = time.monotonic()
    with pytest.raises(HTTPException) as exc:
        ac.acquire(False)
    assert time.monotonic() - t0 < 0.5
    assert exc.value.status_code == 503
    assert _code(exc.value) == "search_queue_full"
    assert exc.value.headers == {"Retry-After": "2"}
    for t in held:
        ac.release(t)
    th.join(2)


def _swallow(fn, *args):
    try:
        return fn(*args)
    except HTTPException:
        return None


def test_queue_wait_over_limit_is_a_503_with_retry_after():
    ac = _controller(queue_wait_seconds=0.2)
    held = [ac.acquire(False), ac.acquire(False)]
    with pytest.raises(HTTPException) as exc:
        ac.acquire(False)
    assert exc.value.status_code == 503
    assert _code(exc.value) == "search_queue_timeout"
    assert exc.value.detail["retryAfter"] == 1  # type: ignore[index]
    assert exc.value.headers == {"Retry-After": "1"}
    assert ac.waiting == 0
    for t in held:
        ac.release(t)


def test_second_large_is_immediate_503_while_normal_proceeds():
    ac = _controller()
    large = ac.acquire(True)
    with pytest.raises(HTTPException) as exc:
        ac.acquire(True)
    assert exc.value.status_code == 503
    assert _code(exc.value) == "large_search_busy"
    assert exc.value.headers == {"Retry-After": "120"}
    normal = ac.acquire(False)          # the second slot is still free
    assert ac.running == 2 and ac.large_running == 1
    for t in (large, normal):
        ac.release(t)
    assert ac.large_running == 0


def test_failed_large_slot_returns_its_large_permit():
    ac = _controller(queue_size=0)
    held = [ac.acquire(False), ac.acquire(False)]
    with pytest.raises(HTTPException):
        ac.acquire(True)                # no slot, no queue -> 503
    assert ac.large_running == 0
    for t in held:
        ac.release(t)


def test_queue_is_fifo():
    ac = _controller(slots=1)
    first = ac.acquire(False)
    order: list[int] = []

    def waiter(i):
        t = ac.acquire(False)
        order.append(i)
        ac.release(t)

    threads = []
    for i in range(3):
        th = threading.Thread(target=waiter, args=(i,))
        th.start()
        threads.append(th)
        assert _wait_until(lambda i=i: ac.waiting == i + 1)
    ac.release(first)
    for th in threads:
        th.join(2)
    assert order == [0, 1, 2]


def test_release_is_idempotent():
    ac = _controller()
    t = ac.acquire(False)
    ac.release(t)
    ac.release(t)
    assert ac.running == 0


def test_policy_limits_and_large_classification():
    p = SearchPolicy(limits_on=True, max_width=3, max_depth=3,
                     large_threshold=4, admission=None, stats_write=False)
    assert SearchPolicy.effective_width(None, 10) == 10
    assert SearchPolicy.effective_width(2, 10) == 2
    p.check_limits(3, 3)
    with pytest.raises(HTTPException) as exc:
        p.check_limits(4, 1)
    assert exc.value.status_code == 422
    assert _code(exc.value) == "search_limit_exceeded"
    assert p.is_large(2, 2) and p.is_large(3, 3)
    assert not p.is_large(1, 3) and not p.is_large(3, 1) and not p.is_large(0, 3)
    off = SearchPolicy(limits_on=False, max_width=3, max_depth=3,
                       large_threshold=4, admission=None, stats_write=True)
    off.check_limits(10, 3)


# -- the commit/abandon race ------------------------------------------------

@pytest.fixture
def engine():
    eng = create_engine("sqlite+pysqlite:///:memory:", poolclass=StaticPool,
                        connect_args={"check_same_thread": False})
    meta = MetaData()
    Table("t", meta, Column("id", Integer, primary_key=True))
    meta.create_all(eng)
    yield eng
    eng.dispose()


def _rows(engine) -> int:
    with engine.connect() as c:
        return c.execute(select(func.count()).select_from(
            Table("t", MetaData(), autoload_with=engine))).scalar_one()


class _OneSlot(AdmissionController):
    def __init__(self):
        super().__init__(slots=1, large_slots=1, queue_size=0,
                         queue_wait_seconds=1, timeout_seconds=1)


def test_timeout_first_means_the_search_never_commits(engine):
    ac = _OneSlot()
    ticket = ac.acquire(False)
    go = threading.Event()

    def work(session, commit):
        go.wait(2)
        session.execute(Table("t", MetaData(), autoload_with=engine).insert())
        commit()
        return "result"

    job = _SearchJob(bind=engine, work=work)
    threading.Thread(target=job.run_and_release, args=(ac, ticket)).start()
    with pytest.raises(HTTPException) as exc:
        job.wait(0.05, 300)
    assert exc.value.status_code == 504
    assert ac.running == 1              # slot held while the search runs
    go.set()
    assert _wait_until(lambda: ac.running == 0)
    assert _rows(engine) == 0           # abandoned -> rolled back


def test_commit_first_means_the_result_is_returned(engine):
    ac = _OneSlot()
    ticket = ac.acquire(False)

    def work(session, commit):
        session.execute(Table("t", MetaData(), autoload_with=engine).insert())
        commit()
        time.sleep(0.3)                 # committed, still finishing up
        return "result"

    job = _SearchJob(bind=engine, work=work)
    threading.Thread(target=job.run_and_release, args=(ac, ticket)).start()
    time.sleep(0.1)                     # let it reach COMMITTING
    assert job.wait(0.05, 300) == "result"
    assert _rows(engine) == 1
    assert ac.running == 0
