"""Builds the production Runtime from settings. Used by the API and the CLI."""

import logging

from parley.adapters.channels.dry_run import DryRunChannel
from parley.adapters.clock import SystemClock
from parley.config import Settings, get_settings
from parley.db.session import make_engine, make_session_factory
from parley.services.runtime import Runtime


def build_runtime(settings: Settings | None = None) -> Runtime:
    settings = settings or get_settings()
    engine = make_engine(settings.database_url)
    return Runtime(
        session_factory=make_session_factory(engine),
        clock=SystemClock(),
        # M1 has no real channel yet; email via SES arrives in M2.
        channel=DryRunChannel(),
    )


def configure_logging(settings: Settings | None = None) -> None:
    settings = settings or get_settings()
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
