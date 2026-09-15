"""Process-level startup state, read by GET /ready (publishing Stage 3)."""
from __future__ import annotations

import threading

_lock = threading.Lock()
_prewarm_finished = False
_prewarm_ok: bool | None = None  # None = skipped (PREWARM_ON_STARTUP=0)


def mark_prewarm_finished(*, ok: bool | None) -> None:
    global _prewarm_finished, _prewarm_ok
    with _lock:
        _prewarm_finished, _prewarm_ok = True, ok


def prewarm_status() -> tuple[bool, bool | None]:
    with _lock:
        return _prewarm_finished, _prewarm_ok
