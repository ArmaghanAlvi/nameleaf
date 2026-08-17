import logging
import os
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes import explore_v2, generate, health, languages, senses

# "uvicorn.error" rather than __name__: uvicorn configures its own loggers,
# and a bare app logger propagates to a root that has no handler under the
# default config -- so the pre-warm timing below would be invisible in the
# server log, which is the one place an operator would look for it.
logger = logging.getLogger("uvicorn.error")

# Pre-warm is ON by default; set PREWARM_ON_STARTUP=0 to skip it. The only
# reason to skip is `uvicorn --reload` in dev, where every code edit restarts
# the process and would re-pay the model load before the port opens.
PREWARM_ON_STARTUP = os.environ.get("PREWARM_ON_STARTUP", "1") != "0"


@asynccontextmanager
async def lifespan(app: FastAPI):
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
    """
    if PREWARM_ON_STARTUP:
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
        except Exception:
            logger.exception("startup pre-warm failed; continuing cold")
    yield


app = FastAPI(
    title="Namecraft API",
    description="Backend API for searching and generating names by meaning.",
    lifespan=lifespan,
)


app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


app.include_router(health.router)
app.include_router(generate.router)
app.include_router(senses.router)
app.include_router(explore_v2.router)
app.include_router(languages.router)