"""
Config for the data pipeline only. No LLM/embedding settings here —
those belong to the serving layer, which isn't part of this package.

Two database URLs on purpose:

  database_url            — used by the app at RUNTIME (pipeline, vector build).
                             Points at Supabase's *transaction pooler* (port 6543).
                             Pgbouncer in transaction mode multiplexes many short
                             connections, which is exactly what a batch pipeline
                             opening/closing sessions wants — but it does NOT
                             support session-level features (server-side prepared
                             statements, LISTEN/NOTIFY, advisory locks across
                             statements). src/db.py disables prepared-statement
                             caching to stay compatible with this.

  database_migration_url  — used ONLY for schema migrations (alembic) and the
                             `CREATE EXTENSION vector` bootstrap. Points at
                             Supabase's *session pooler* or *direct connection*
                             (port 5432), which supports DDL and session state
                             the transaction pooler doesn't. Falls back to
                             database_url with the port swapped if not set
                             explicitly, so a single .env still works.
"""
from functools import lru_cache
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    environment: Literal["development", "staging", "production"] = "development"
    log_level: str = "INFO"
    log_format: Literal["text", "json"] = "text"

    # --- Database (Supabase Postgres) -------------------------------------
    # Runtime traffic — Supabase transaction pooler, port 6543.
    database_url: str = "postgresql+psycopg://postgres:postgres@localhost:5432/real_estate"
    # Migrations/DDL — Supabase session pooler / direct connection, port 5432.
    # Optional: if unset, derived from database_url by src/db.py.
    database_migration_url: str = ""

    db_pool_size: int = 5
    db_max_overflow: int = 5
    db_pool_timeout_seconds: int = 30
    db_pool_recycle_seconds: int = 1800  # recycle before Supabase's idle-connection timeout
    db_statement_timeout_ms: int = 60_000
    db_connect_timeout_seconds: int = 10
    db_sslmode: str = "require"  # Supabase requires TLS

    # --- Scraper (listings search index) -------------------------------------
    # All source-specific settings come from the environment (.env), never
    # from code defaults — see .env.example for the full list.
    scraper_app_id: str = ""
    scraper_api_key: str = ""
    scraper_index_name: str = ""
    # Endpoint template for the search index API, e.g.
    # "https://{app_id}-1.example-api.net/1/indexes/*/queries" — the {app_id}
    # placeholder is replaced at runtime. Set in .env.
    scraper_base_url_template: str = ""
    # User-agent header sent with search requests.
    scraper_agent: str = "listings-scraper-custom"
    # Template used to build a listing's public URL from its external id,
    # e.g. "https://www.example-portal.com/property/details-{external_id}.html".
    # The {external_id} placeholder is replaced at runtime. Set in .env;
    # empty disables URL building (url columns stay NULL).
    listing_url_template: str = ""

    # --- Retry / resilience -------------------------------------------------
    db_retry_attempts: int = 3
    db_retry_min_wait_seconds: float = 1.0
    db_retry_max_wait_seconds: float = 8.0

    # Only needed by src/vector/ (embedding step) — leave blank if not using it yet
    nvidia_api_key: str = ""
    nvidia_base_url: str = "https://integrate.api.nvidia.com/v1"
    embedding_model: str = "nvidia/nemotron-3-embed-1b"
    embedding_dim: int = 2048

    @field_validator("database_url", "database_migration_url")
    @classmethod
    def _reject_placeholder_password(cls, v: str) -> str:
        if v and "[YOUR-PASSWORD]" in v:
            raise ValueError(
                "DATABASE_URL still contains the literal '[YOUR-PASSWORD]' placeholder — "
                "replace it with your real Supabase database password in .env."
            )
        return v

    @property
    def is_production(self) -> bool:
        return self.environment == "production"


@lru_cache
def get_settings() -> Settings:
    return Settings()
