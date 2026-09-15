import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import runtime_state
from app.api.routes import explore_v2, generate, health, languages, senses
from app.config import Settings, settings
from app.middleware.rate_limit import RateLimitMiddleware, RateRule
from app.observability import init_sentry

# "uvicorn.error" rather than __name__: uvicorn configures its own loggers,
# and a bare app logger propagates to a root that has no handler under the
# default config -- so the pre-warm timing below would be invisible in the
# server log, which is the one place an operator would look for it.
logger = logging.getLogger("uvicorn.error")


def _prewarm() -> None:
    """Pay the per-process warm-up here instead of on the first user request.

    None of this reduces total work -- it moves it off the request that
    happens to arrive first. What it covers, in order of cost:

      * the embedding model. get_model() loads weights; the first forward
        pass separately pays MPS graph compilation, which is why an actual
        embed_query call is needed and get_model() alone is not enough
        (same reason scripts/eval/phase_timing.py warms both before timing).
      * the language directory and pivot-eligibility caches, both
        module-level globals populated on first use. Since
        languages.has_wordnet_edges is persisted (a1c7f3e94b28) the second
        is now cheap, but it still costs a query per process.

    Failure here must never take the server down: a warm-up is an
    optimization, and the same work will happen lazily on first use anyway.
    Set PREWARM_ON_STARTUP=0 to skip it; the only reason to skip is
    `uvicorn --reload` in dev, where every code edit restarts the process.
    """
    t0 = time.perf_counter()
    try:
        from app.db.session import SessionLocal
        from app.services.embedding_provider import embed_query, get_model
        from app.services.language_directory import visible_languages
        from app.services.parallel_expansion import _pivot_eligible_languages

        get_model()
        embed_query("warmup")
        with SessionLocal() as db:
            visible_languages(db)
            _pivot_eligible_languages(db)
        logger.info("startup pre-warm finished in %.2fs",
                    time.perf_counter() - t0)
        runtime_state.mark_prewarm_finished(ok=True)
    except Exception:
        logger.exception("startup pre-warm failed; continuing cold")
        runtime_state.mark_prewarm_finished(ok=False)


def _fence_llm_if_production(cfg: Settings) -> bool:
    """Production never makes query-time Gemini calls unless explicitly
    allowed: the trickle writes root_llm_attempts and sense_translations,
    reference tables that publishing replaces (CLAUDE.md). Uses the same
    fence the eval harnesses use, so there is one mechanism, not two."""
    if cfg.is_production and not cfg.allow_query_time_llm_in_production:
        from app.services.root_llm import fence_query_time_llm

        fence_query_time_llm()
        return True
    return False


def create_app(cfg: Settings = settings) -> FastAPI:
    init_sentry(cfg)
    llm_fenced = _fence_llm_if_production(cfg)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        logger.info(
            "posture: app_env=%s docs=%s include_hidden=%s rate_limit=%s "
            "query_time_llm_fenced=%s sentry=%s",
            cfg.app_env, cfg.docs_enabled, cfg.include_hidden_allowed,
            cfg.rate_limiting_on, llm_fenced, bool(cfg.sentry_dsn),
        )
        if cfg.prewarm_on_startup:
            _prewarm()
        else:
            runtime_state.mark_prewarm_finished(ok=None)
        yield

    docs = cfg.docs_enabled
    app = FastAPI(
        title="Namecraft API",
        description="Backend API for searching and generating names by meaning.",
        lifespan=lifespan,
        docs_url="/docs" if docs else None,
        redoc_url="/redoc" if docs else None,
        openapi_url="/openapi.json" if docs else None,
    )
    app.state.settings = cfg

    app.add_middleware(
        CORSMiddleware,
        allow_origins=cfg.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    if cfg.rate_limiting_on:
        app.add_middleware(
            RateLimitMiddleware,
            rules=[
                RateRule("/explore-v2", cfg.rate_limit_explore_per_minute, 60),
                RateRule("/senses/lookup", cfg.rate_limit_lookup_per_minute, 60),
                RateRule("/generate", cfg.rate_limit_lookup_per_minute, 60),
            ],
        )

    app.include_router(health.router)
    app.include_router(generate.router)
    app.include_router(senses.router)
    app.include_router(explore_v2.router)
    app.include_router(languages.router)
    return app


app = create_app()
