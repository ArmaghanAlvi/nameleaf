from fastapi import APIRouter, Depends, Response
from sqlalchemy import text
from sqlalchemy.orm import Session

from app import runtime_state
from app.db.session import get_db

router = APIRouter(tags=["health"])


@router.get("/health")
def health_check():
    """Liveness: the process is up. Never touches the database."""
    return {"status": "ok"}


@router.get("/ready")
def readiness_check(response: Response, db: Session = Depends(get_db)):
    """Readiness: the startup pre-warm SUCCEEDED (or was skipped) and the
    database answers. The production health check and uptime monitor use
    this, so a backend that can't reach Postgres is reported as unhealthy
    instead of looking fine while every search fails.

    A failed pre-warm is not ready (A-C.1, B5): that backend would pay the
    whole warm-up on some user's first search. It is not permanent either --
    main.py retries the pre-warm in the background and this flips to ready
    when a retry succeeds."""
    prewarm_finished, prewarm_ok = runtime_state.prewarm_status()
    try:
        db.execute(text("SELECT 1"))
        database_ok = True
    except Exception:
        database_ok = False
    ready = prewarm_finished and prewarm_ok is not False and database_ok
    if not ready:
        response.status_code = 503
    return {
        "status": "ready" if ready else "not_ready",
        "prewarmFinished": prewarm_finished,
        "prewarmOk": prewarm_ok,
        "prewarmAttempts": runtime_state.prewarm_attempts(),
        "database": database_ok,
    }
