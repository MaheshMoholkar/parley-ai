"""Settings read from environment variables (or a `.env` file), each prefixed PARLEY_.

Example: PARLEY_DATABASE_URL=postgresql+psycopg://user:pass@host:5432/parley
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PARLEY_", env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://parley:parley@localhost:5432/parley"
    # How often the worker pulls invoices from each tenant's source system.
    sync_interval_minutes: int = 60
    # How long the worker sleeps between rounds when there is nothing to do.
    worker_poll_seconds: int = 30
    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    return Settings()
