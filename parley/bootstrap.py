"""Builds the production Runtime from settings. Used by the API and the CLI."""

import logging

from parley.adapters.channels.dry_run import DryRunChannel
from parley.adapters.channels.email_ses import SesEmailChannel
from parley.adapters.clock import SystemClock
from parley.adapters.models.bedrock import BedrockModel
from parley.adapters.models.bedrock_agent import BedrockAgentModel
from parley.config import Settings, get_settings
from parley.db.session import make_engine, make_session_factory
from parley.ports.channel import ChannelPort
from parley.services.runtime import Runtime


def build_runtime(settings: Settings | None = None) -> Runtime:
    settings = settings or get_settings()
    engine = make_engine(settings.database_url)
    model = None
    agent_model = None
    if settings.model_provider == "bedrock":
        agent_model = BedrockAgentModel(
            settings.aws_region, settings.model_large, settings.effort_large
        )
        model = BedrockModel(
            region=settings.aws_region,
            model_ids={"large": settings.model_large, "small": settings.model_small},
            effort={"large": settings.effort_large, "small": settings.effort_small},
        )
    channel: ChannelPort = DryRunChannel()
    if settings.channel == "ses":
        channel = SesEmailChannel(settings.email_from, settings.reply_domain, settings.aws_region)
    return Runtime(
        session_factory=make_session_factory(engine),
        clock=SystemClock(),
        channel=channel,
        model=model,
        agent_model=agent_model,
        inbound_secret=settings.inbound_secret,
        model_prices=settings.model_prices,
    )


def configure_logging(settings: Settings | None = None) -> None:
    settings = settings or get_settings()
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
