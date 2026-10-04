from fastapi import APIRouter, Request

router = APIRouter(tags=["search-limits"])


@router.get("/search-limits")
def search_limits(request: Request) -> dict:
    """The UI's single source for slider maxima and the large-search note
    (B5), so raising SEARCH_MAX_WIDTH is a settings change only and the UI
    and the API can't silently disagree. Always the configured numbers;
    the two flags say whether this server enforces them. No database."""
    cfg = request.app.state.settings
    return {
        "maxWidth": cfg.search_max_width,
        "maxDepth": cfg.search_max_depth,
        "largeThreshold": cfg.search_large_threshold,
        "limitsEnforced": cfg.search_limits_on,
        "admissionEnforced": cfg.search_admission_on,
    }
