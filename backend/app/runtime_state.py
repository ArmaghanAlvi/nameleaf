"""Process-level startup state, read by GET /ready (publishing Stage 3)."""
from __future__ import annotations

import threading

_lock = threading.Lock()
_prewarm_finished = False
_prewarm_ok: bool | None = None  # None = skipped (PREWARM_ON_STARTUP=0)
_prewarm_attempts = 0


def mark_prewarm_finished(*, ok: bool | None) -> None:
    """Record one pre-warm outcome. A failure is NOT final (B5, A-C.1): the
    background retry calls this again with ok=True once a retry succeeds."""
    global _prewarm_finished, _prewarm_ok, _prewarm_attempts
    with _lock:
        _prewarm_finished, _prewarm_ok = True, ok
        if ok is not None:
            _prewarm_attempts += 1


def prewarm_status() -> tuple[bool, bool | None]:
    with _lock:
        return _prewarm_finished, _prewarm_ok


def prewarm_attempts() -> int:
    with _lock:
        return _prewarm_attempts
