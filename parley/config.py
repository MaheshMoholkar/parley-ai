"""Settings read from environment variables (or a `.env` file), each prefixed PARLEY_.

Example: PARLEY_DATABASE_URL=postgresql+psycopg://user:pass@host:5432/parley
"""

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

Effort = Literal["low", "medium", "high", "xhigh", "max"]


class Settings(BaseSettings):
    # protected_namespaces=() allows field names that start with "model_".
    model_config = SettingsConfigDict(
        env_prefix="PARLEY_", env_file=".env", extra="ignore", protected_namespaces=()
    )

    database_url: str = "postgresql+psycopg://parley:parley@localhost:5432/parley"
    # How often the worker pulls invoices from each tenant's source system.
    sync_interval_minutes: int = 60
    # How long the worker sleeps between rounds when there is nothing to do.
    worker_poll_seconds: int = 30
    log_level: str = "INFO"

    # --- Email ---
    # "dry_run" records messages instead of sending; "ses" sends with Amazon SES.
    channel: Literal["dry_run", "ses"] = "dry_run"
    email_from: str = "reminders@example.com"
    # Replies go to reply+<token>@<reply_domain>, received by SES.
    reply_domain: str = "replies.example.com"
    # Shared secret that signs POST /v1/inbound/email. Inbound mail is refused
    # while this is empty.
    inbound_secret: str = ""

    # --- Model (Claude on Amazon Bedrock) ---
    # "template" uses the fixed reminder template and no model (M1 behaviour);
    # "bedrock" drafts with the model and reads replies.
    model_provider: Literal["template", "bedrock"] = "template"
    aws_region: str = "ap-south-1"
    model_large: str = "anthropic.claude-opus-5-5"
    model_small: str = "anthropic.claude-sonnet-5-5"
    effort_large: Effort = "medium"
    effort_small: Effort = "low"
    # USD per million tokens (input, output) for cost estimates. These are the
    # Anthropic list prices; Bedrock pricing can differ, so set your own.
    model_prices: dict[str, tuple[float, float]] = {
        "anthropic.claude-opus-5-5": (4.0, 20.0),
        "anthropic.claude-sonnet-5-5": (2.0, 10.0),
    }


@lru_cache
def get_settings() -> Settings:
    return Settings()
