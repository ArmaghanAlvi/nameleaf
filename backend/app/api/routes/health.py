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
    """Readiness: the startup pre-warm has run (or was skipped) and the
    database answers. The production health check and uptime monitor use
    this, so a backend that can't reach Postgres is reported as unhealthy
    instead of looking fine while every search fails."""
    prewarm_finished, prewarm_ok = runtime_state.prewarm_status()
    try:
        db.execute(text("SELECT 1"))
        database_ok = True
    except Exception:
        database_ok = False
    ready = prewarm_finished and database_ok
    if not ready:
        response.status_code = 503
    return {
        "status": "ready" if ready else "not_ready",
        "prewarmFinished": prewarm_finished,
        "prewarmOk": prewarm_ok,
        "database": database_ok,
    }
