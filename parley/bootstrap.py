"""Builds the production Runtime from settings. Used by the API and the CLI."""

import logging

from parley.adapters.channels.dry_run import DryRunChannel
from parley.adapters.channels.email_ses import SesEmailChannel
from parley.adapters.channels.voice.twilio import TwilioVoice
from parley.adapters.clock import SystemClock
from parley.adapters.models.bedrock import BedrockModel
from parley.adapters.models.bedrock_agent import BedrockAgentModel
from parley.adapters.models.nova_sonic import NovaSonic
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
    speech = None
    if settings.speech_provider == "nova_sonic":
        speech = NovaSonic(settings.aws_region, settings.speech_model_id, settings.voice_ids)
    voice = None
    if settings.voice == "twilio":
        if not settings.public_url.startswith("https://"):
            raise ValueError("PARLEY_PUBLIC_URL must be this service's https address for Twilio")
        voice = TwilioVoice(
            settings.twilio_account_sid,
            settings.twilio_auth_token,
            settings.twilio_from_number,
            settings.public_url,
        )
    return Runtime(
        session_factory=make_session_factory(engine),
        clock=SystemClock(),
        channel=channel,
        model=model,
        agent_model=agent_model,
        inbound_secret=settings.inbound_secret,
        model_prices=settings.model_prices,
        voice=voice,
        speech=speech,
        voice_allowed_numbers=frozenset(settings.voice_allowed_numbers),
        voice_max_seconds=settings.voice_max_seconds,
    )


def configure_logging(settings: Settings | None = None) -> None:
    settings = settings or get_settings()
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
