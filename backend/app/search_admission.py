"""Search limits and admission control for POST /explore-v2 (publishing B5).

ORDER IN THE ROUTE: validate limits (422) -> [future result-cache seam] ->
admission (503) -> record the sense selection -> search -> attach name cards
-> commit. Everything here is OFF locally (settings unset): the route then
calls the search inline in the request's session, exactly as before B5.

WHY THREADS, NOT ASYNC: the route is a sync `def`, so Starlette runs it in
its threadpool, and the engine is synchronous SQLAlchemy. A Condition-based
controller is the smallest thing that fits.

KEEP-THE-SLOT. The engine cannot be cancelled mid-search (that would need
changes inside parallel_expand), so a timed-out search keeps running to
completion. The rule that makes this safe: the slot (and the large permit) is
released by the SEARCH thread when it finishes, never by the request thread
when it gives up. The request returns 504; the server's concurrency never
exceeds its slots. An abandoned search must also never commit, and a
finished one must never be thrown away: one lock decides which side won (see
_SearchJob).

SESSION LIFETIME. A request that returns 504 while its search continues would
have its request-scoped session closed under it by FastAPI. So the search
runs in its OWN session, bound to the same engine as the request's session.
Deriving the bind from the request session is what makes test dependency
overrides (SQLite) and the eval harness's direct calls work unchanged.

Error bodies (422 / 503 / 504) share one shape, documented in CLAUDE.md:
  {"detail": {"code": str, "message": str, "retryAfter": int  # 503 only}}
FastAPI's own schema-validation 422 keeps `detail` as a LIST, so clients can
tell the two apart.
"""
from __future__ import annotations

import logging
import math
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

from fastapi import HTTPException, Request
from sqlalchemy.orm import Session

from app.config import Settings

logger = logging.getLogger("uvicorn.error")

# A large search holds its lane for minutes; tell the client so.
LARGE_BUSY_RETRY_AFTER_SECONDS = 120


def search_error(status: int, code: str, message: str,
                 retry_after: int | None = None) -> HTTPException:
    detail: dict[str, Any] = {"code": code, "message": message}
    headers = None
    if retry_after is not None:
        detail["retryAfter"] = retry_after
        headers = {"Retry-After": str(retry_after)}
    return HTTPException(status_code=status, detail=detail, headers=headers)


@dataclass(eq=False)
class Ticket:
    large: bool
    released: bool = field(default=False)


class AdmissionController:
    """`slots` searches run at once, at most `large_slots` of them large.

    Normal slots queue FIFO, up to `queue_size` waiting for up to
    `queue_wait_seconds`. A large search first takes a large permit (none
    free -> immediate 503, no large queue), then a normal slot under the same
    queue rules.
    """

    def __init__(self, *, slots: int, large_slots: int, queue_size: int,
                 queue_wait_seconds: float, timeout_seconds: float,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.slots = slots
        self.large_slots = large_slots
        self.queue_size = queue_size
        self.queue_wait_seconds = queue_wait_seconds
        self.timeout_seconds = timeout_seconds
        self._clock = clock
        self._cond = threading.Condition()
        self._running = 0
        self._large_running = 0
        self._queue: deque[object] = deque()

    # -- introspection (posture log, tests) --------------------------------
    @property
    def running(self) -> int:
        with self._cond:
            return self._running

    @property
    def large_running(self) -> int:
        with self._cond:
            return self._large_running

    @property
    def waiting(self) -> int:
        with self._cond:
            return len(self._queue)

    # -- slots --------------------------------------------------------------
    def _queue_retry_after(self) -> int:
        return max(1, math.ceil(self.queue_wait_seconds))

    def _take_slot(self) -> None:
        """Called with the condition held."""
        if self._running < self.slots and not self._queue:
            self._running += 1
            return
        if len(self._queue) >= self.queue_size:
            raise search_error(
                503, "search_queue_full",
                "The server is busy with other searches. "
                "Please try again in a moment.",
                self._queue_retry_after())
        me = object()
        self._queue.append(me)
        deadline = self._clock() + self.queue_wait_seconds
        try:
            while not (self._queue[0] is me and self._running < self.slots):
                remaining = deadline - self._clock()
                if remaining <= 0:
                    raise search_error(
                        503, "search_queue_timeout",
                        "The server is still busy with other searches. "
                        "Please try again in a moment.",
                        self._queue_retry_after())
                self._cond.wait(remaining)
            self._queue.popleft()
            self._running += 1
        finally:
            if me in self._queue:
                self._queue.remove(me)
            # The head may have changed, or a second slot may be free.
            self._cond.notify_all()

    def acquire(self, large: bool) -> Ticket:
        with self._cond:
            if large:
                if self._large_running >= self.large_slots:
                    raise search_error(
                        503, "large_search_busy",
                        "A large search is already running. "
                        "Try again in a few minutes.",
                        LARGE_BUSY_RETRY_AFTER_SECONDS)
                self._large_running += 1
            try:
                self._take_slot()
            except BaseException:
                if large:
                    self._large_running -= 1
                    self._cond.notify_all()
                raise
            return Ticket(large=large)

    def release(self, ticket: Ticket) -> None:
        with self._cond:
            if ticket.released:
                return
            ticket.released = True
            self._running -= 1
            if ticket.large:
                self._large_running -= 1
            self._cond.notify_all()

    # -- the whole admitted search -------------------------------------------
    def run(self, *, request_db: Session, large: bool,
            work: Callable[[Session, Callable[[], None]], Any]) -> Any:
        """Admit, run `work(session, commit)` on a search thread with its own
        session, and wait for it up to the overall timeout.

        The timeout is measured from here, so queue time counts against it.
        """
        deadline = self._clock() + self.timeout_seconds
        ticket = self.acquire(large)
        job = _SearchJob(bind=request_db.get_bind(), work=work)
        try:
            threading.Thread(target=job.run_and_release, args=(self, ticket),
                             name="search-worker", daemon=True).start()
        except BaseException:
            self.release(ticket)
            raise
        return job.wait(deadline - self._clock(), self.timeout_seconds)


_RUNNING, _COMMITTING, _ABANDONED = "running", "committing", "abandoned"


class _SearchJob:
    """One admitted search. The request thread and the search thread race
    exactly once -- timeout versus commit -- and `_lock` settles it:

      search thread, about to commit:  ABANDONED -> roll back, never commit
                                       otherwise -> COMMITTING, commit
      request thread, on timeout:      RUNNING   -> ABANDONED, return 504
                                       COMMITTING -> wait; the result is
                                       moments away, so return it
    """

    def __init__(self, *, bind: Any,
                 work: Callable[[Session, Callable[[], None]], Any]) -> None:
        self._bind = bind
        self._work = work
        self._lock = threading.Lock()
        self._state = _RUNNING
        self._done = threading.Event()
        self._result: Any = None
        self._error: BaseException | None = None

    def _commit(self, session: Session) -> None:
        with self._lock:
            if self._state == _ABANDONED:
                session.rollback()
                return
            self._state = _COMMITTING
        session.commit()

    def run_and_release(self, admission: AdmissionController,
                        ticket: Ticket) -> None:
        session = Session(bind=self._bind, autoflush=False)
        try:
            self._result = self._work(session, lambda: self._commit(session))
        except BaseException as exc:  # noqa: BLE001 -- handed to the waiter
            self._error = exc
            with self._lock:
                abandoned = self._state == _ABANDONED
            if abandoned:
                logger.exception("abandoned search failed after its 504")
        finally:
            try:
                session.close()  # rolls back anything uncommitted
            finally:
                # Only now, with the search finished, does the slot free up.
                admission.release(ticket)
                self._done.set()

    def _outcome(self) -> Any:
        if self._error is not None:
            raise self._error
        return self._result

    def wait(self, remaining: float, timeout_seconds: float) -> Any:
        if self._done.wait(max(remaining, 0.0)):
            return self._outcome()
        with self._lock:
            if self._state == _RUNNING:
                self._state = _ABANDONED
                minutes = timeout_seconds / 60
                raise search_error(
                    504, "search_timeout",
                    f"This search took longer than {minutes:g} minutes. "
                    "Try a smaller breadth or depth.")
        self._done.wait()
        return self._outcome()


@dataclass(frozen=True)
class SearchPolicy:
    limits_on: bool
    max_width: int
    max_depth: int
    large_threshold: int
    admission: AdmissionController | None

    @classmethod
    def from_settings(cls, cfg: Settings) -> "SearchPolicy":
        admission = None
        if cfg.search_admission_on:
            admission = AdmissionController(
                slots=cfg.search_concurrency,
                large_slots=cfg.search_large_concurrency,
                queue_size=cfg.search_queue_size,
                queue_wait_seconds=cfg.search_queue_wait_seconds,
                timeout_seconds=cfg.search_timeout_seconds,
            )
        return cls(
            limits_on=cfg.search_limits_on,
            max_width=cfg.search_max_width,
            max_depth=cfg.search_max_depth,
            large_threshold=cfg.search_large_threshold,
            admission=admission,
        )

    @staticmethod
    def effective_width(width: int | None, expansion_count: int) -> int:
        """The width the engine will actually use: `width`, or
        `expansionCount` when `width` is absent (the legacy path's input)."""
        return width if width is not None else expansion_count

    def is_large(self, width: int, depth: int) -> bool:
        return width * depth >= self.large_threshold

    def check_limits(self, width: int, depth: int) -> None:
        if not self.limits_on:
            return
        if width > self.max_width:
            raise search_error(
                422, "search_limit_exceeded",
                f"Breadth {width} is above this server's maximum of "
                f"{self.max_width}.")
        if depth > self.max_depth:
            raise search_error(
                422, "search_limit_exceeded",
                f"Depth {depth} is above this server's maximum of "
                f"{self.max_depth}.")


def get_search_policy(request: Request) -> SearchPolicy:
    return request.app.state.search_policy


@lru_cache(maxsize=1)
def _process_policy() -> SearchPolicy:
    from app import config

    return SearchPolicy.from_settings(config.settings)


def resolve_policy(value: object) -> SearchPolicy:
    """The route's injected policy, or -- when the route is called directly as
    a function (the eval harnesses do this), so FastAPI injected nothing --
    one built from the process settings."""
    return value if isinstance(value, SearchPolicy) else _process_policy()
