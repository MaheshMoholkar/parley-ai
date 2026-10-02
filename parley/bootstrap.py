"""Builds the production Runtime from settings. Used by the API and the CLI."""

import logging

from parley.adapters.channels.dry_run import DryRunChannel
from parley.adapters.clock import SystemClock
from parley.adapters.models.bedrock import BedrockModel
from parley.config import Settings, get_settings
from parley.db.session import make_engine, make_session_factory
from parley.services.runtime import Runtime


def build_runtime(settings: Settings | None = None) -> Runtime:
    settings = settings or get_settings()
    engine = make_engine(settings.database_url)
    model = None
    if settings.model_provider == "bedrock":
        model = BedrockModel(
            region=settings.aws_region,
            model_ids={"large": settings.model_large, "small": settings.model_small},
            effort={"large": settings.effort_large, "small": settings.effort_small},
        )
    return Runtime(
        session_factory=make_session_factory(engine),
        clock=SystemClock(),
        channel=DryRunChannel(),
        model=model,
        model_prices=settings.model_prices,
    )


def configure_logging(settings: Settings | None = None) -> None:
    settings = settings or get_settings()
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
