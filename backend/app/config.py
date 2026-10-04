from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


BACKEND_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """Every production knob lives here (publishing Stage 3a).

    Security-relevant flags derive from APP_ENV, so production is safe with a
    single setting: APP_ENV=production turns API docs off, stops honoring
    includeHidden, turns rate limiting on, and fences the query-time LLM.
    Each can still be overridden explicitly. APP_ENV=local (the default)
    behaves exactly as the app did before this stage.

    Deliberately NOT here: ROOT_LLM_* are read by services/root_llm.py
    directly, because the harness fence works through that module's
    globals. RANKING_STATS_WRITE arrives in Stage 2.

    EMBEDDING_DEVICE / TORCH_NUM_THREADS (Stage 1a) pin the embedding model's
    device and torch's intra-op thread count. Unset keeps today's behaviour:
    auto-detect mps, then cuda, then cpu, with torch's default thread count.
    An explicit device that isn't available fails at model load rather than
    falling back, so a measurement can never carry the wrong label. The CPU
    configuration that gates certify production in is
    EMBEDDING_DEVICE=cpu TORCH_NUM_THREADS=2.

    SEARCH_* (Stage 1f, B5) bound what one search may cost and how many run
    at once. Two switches, each derived from APP_ENV like rate limiting:
    SEARCH_LIMITS_ENABLED (422 for width/depth above the maxima) and
    SEARCH_ADMISSION_ENABLED (slots, the large-search lane, the queue and
    the timeout; see app/search_admission.py). The numeric values ARE the
    production values; locally both switches are off and the request path
    is exactly the pre-B5 code.

    RANKING_STATS_WRITE (Stage 2b, C2) governs every usage-statistics write
    (POST /explore-v2's record_sense_selection is the only writer). On
    locally, off in production; an explicit value wins. Search results never
    read statistics (cache plan Part A), so this only affects the dropdown.

    Leave optional flags UNSET rather than empty in env files: an empty
    string is not a valid boolean.
    """

    database_url: str
    sql_echo: bool = False

    app_env: Literal["local", "production"] = "local"

    # Comma-separated. Production serves the frontend and the API from one
    # origin through Caddy, so CORS only matters for local development.
    cors_origins: str = "http://localhost:3000,http://127.0.0.1:3000"

    prewarm_on_startup: bool = True

    # None = derive from app_env (see the properties below).
    expose_api_docs: bool | None = None
    allow_include_hidden: bool | None = None
    rate_limit_enabled: bool | None = None

    rate_limit_explore_per_minute: int = 20
    rate_limit_lookup_per_minute: int = 120

    # The query-time trickle writes to reference tables that publishing
    # replaces (CLAUDE.md), so production fences it unless this is set.
    allow_query_time_llm_in_production: bool = False

    sentry_dsn: str | None = None

    # None = auto-detect / torch default (see the docstring).
    embedding_device: Literal["cpu", "mps", "cuda"] | None = None
    torch_num_threads: int | None = Field(default=None, ge=1)

    # None = derive from app_env (see the docstring and the properties).
    search_limits_enabled: bool | None = None
    # Width is EFFECTIVE width: `width`, or `expansionCount` when absent.
    search_max_width: int = Field(default=3, ge=0)
    search_max_depth: int = Field(default=3, ge=0)

    search_admission_enabled: bool | None = None
    # width x depth at or above this is a "large" search.
    search_large_threshold: int = Field(default=4, ge=1)
    search_concurrency: int = Field(default=2, ge=1)
    search_large_concurrency: int = Field(default=1, ge=1)
    search_queue_size: int = Field(default=4, ge=0)
    search_queue_wait_seconds: float = Field(default=30.0, gt=0)
    # Measured from admission start, so time spent queued counts against it.
    search_timeout_seconds: float = Field(default=300.0, gt=0)

    # None = derive from app_env: on locally, off in production.
    ranking_stats_write: bool | None = None

    model_config = SettingsConfigDict(
        env_file=BACKEND_DIR / ".env",
        extra="ignore",
    )

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @property
    def docs_enabled(self) -> bool:
        if self.expose_api_docs is not None:
            return self.expose_api_docs
        return not self.is_production

    @property
    def include_hidden_allowed(self) -> bool:
        if self.allow_include_hidden is not None:
            return self.allow_include_hidden
        return not self.is_production

    @property
    def rate_limiting_on(self) -> bool:
        if self.rate_limit_enabled is not None:
            return self.rate_limit_enabled
        return self.is_production

    @property
    def ranking_stats_write_on(self) -> bool:
        if self.ranking_stats_write is not None:
            return self.ranking_stats_write
        return not self.is_production

    @property
    def search_limits_on(self) -> bool:
        if self.search_limits_enabled is not None:
            return self.search_limits_enabled
        return self.is_production

    @property
    def search_admission_on(self) -> bool:
        if self.search_admission_enabled is not None:
            return self.search_admission_enabled
        return self.is_production

    @model_validator(mode="after")
    def _large_lane_leaves_room(self) -> "Settings":
        # B-4.4: a large search must never block a normal one, so at least
        # one slot always stays free of large searches.
        if (self.search_admission_on
                and self.search_large_concurrency >= self.search_concurrency):
            raise ValueError(
                "SEARCH_LARGE_CONCURRENCY must be less than "
                "SEARCH_CONCURRENCY when search admission is on"
            )
        return self

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


settings = Settings()  # type: ignore[call-arg]
