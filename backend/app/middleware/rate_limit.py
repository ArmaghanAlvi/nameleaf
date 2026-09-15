"""
Per-IP sliding-window rate limiting for expensive endpoints (publishing 3c).

In-process on purpose: production runs ONE uvicorn worker (each worker loads
its own embedding model), so an in-memory window is exact and needs no Redis
and no new dependency. If workers ever exceed one, each keeps its own window
and the effective limit multiplies -- revisit then.

The client address comes from scope["client"]. Behind Caddy that is the real
visitor only because uvicorn runs with --proxy-headers AND the backend has no
published port (only Caddy can reach it). Without --proxy-headers, every
visitor shares Caddy's address: one bucket for the whole internet.

Off by default locally (APP_ENV=local): the gate captures and
scripts/eval/http_concurrency.py fire hundreds of requests and would trip it.
"""
from __future__ import annotations

import math
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send


@dataclass(frozen=True)
class RateRule:
    path_prefix: str
    max_requests: int
    window_seconds: float


class SlidingWindowLimiter:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._hits: dict[tuple[str, str], deque[float]] = {}
        self._lock = threading.Lock()
        self._allowed = 0

    def check(self, rule: RateRule, client: str) -> float | None:
        """Record an allowed hit and return None, or return seconds until retry.

        Blocked requests are NOT recorded, so hammering while blocked doesn't
        extend the penalty.
        """
        now = self._clock()
        key = (rule.path_prefix, client)
        with self._lock:
            window = self._hits.setdefault(key, deque())
            cutoff = now - rule.window_seconds
            while window and window[0] <= cutoff:
                window.popleft()
            if len(window) >= rule.max_requests:
                return max(window[0] + rule.window_seconds - now, 0.0)
            window.append(now)
            self._allowed += 1
            if self._allowed % 1000 == 0:
                self._sweep(now)
            return None

    def _sweep(self, now: float) -> None:
        stale = [k for k, w in self._hits.items() if not w or w[-1] < now - 3600]
        for k in stale:
            del self._hits[k]


class RateLimitMiddleware:
    def __init__(self, app: ASGIApp, rules: list[RateRule],
                 limiter: SlidingWindowLimiter | None = None) -> None:
        self.app = app
        self.rules = rules
        self.limiter = limiter or SlidingWindowLimiter()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope.get("method") != "OPTIONS":
            rule = self._match(scope.get("path", ""))
            if rule is not None:
                client = scope.get("client")
                retry = self.limiter.check(rule, client[0] if client else "unknown")
                if retry is not None:
                    response = JSONResponse(
                        {"detail": "Too many requests. Please wait a moment and try again."},
                        status_code=429,
                        headers={"Retry-After": str(max(1, math.ceil(retry)))},
                    )
                    await response(scope, receive, send)
                    return
        await self.app(scope, receive, send)

    def _match(self, path: str) -> RateRule | None:
        for rule in self.rules:
            if path == rule.path_prefix or path.startswith(rule.path_prefix + "/"):
                return rule
        return None
