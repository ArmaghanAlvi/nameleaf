from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.schemas.senses import SenseLookupResponse
from app.services.sense_lookup import lookup_sense_options


router = APIRouter(prefix="/senses", tags=["senses"])


@router.get("/lookup", response_model=SenseLookupResponse)
def lookup_senses(
    request: Request,
    query: str = Query(min_length=1),
    languageCode: str | None = None,
    includeHidden: bool = False,
    limit: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
) -> SenseLookupResponse:
    # Hidden senses are a review/admin concept. Production ignores the flag
    # (publishing Stage 3c) so ?includeHidden=true on a public URL can't
    # reveal them. Read from app.state so tests can build a production app.
    allowed = request.app.state.settings.include_hidden_allowed
    options = lookup_sense_options(
        db,
        query=query,
        language_code=languageCode,
        include_hidden=includeHidden and allowed,
        limit=limit,
    )

    return SenseLookupResponse(
        query=query,
        options=options,
    )