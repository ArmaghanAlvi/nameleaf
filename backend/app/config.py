from pathlib import Path
from typing import Literal

from pydantic import Field
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
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


settings = Settings()  # type: ignore[call-arg]
