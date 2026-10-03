"""The things every service needs, bundled so tests can swap any of them.

Production builds a Runtime with the real clock and channel; tests build one
with a FakeClock and a DryRunChannel.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from harness import AgentModel, Limits
from parley.adapters.webhook_http import post_webhook
from parley.db.models import Tenant
from parley.db.session import SessionFactory
from parley.ports.accounting import AccountingPort
from parley.ports.channel import ChannelPort
from parley.ports.clock import Clock
from parley.ports.model import ModelPort
from parley.ports.voice import CallPlacer, SpeechModel
from parley.services.accounting import accounting_adapter_for


@dataclass
class Runtime:
    session_factory: SessionFactory
    clock: Clock
    channel: ChannelPort
    accounting_for: Callable[[Tenant], AccountingPort] = field(default=accounting_adapter_for)
    # None means "no model": reminders use the fixed template.
    model: ModelPort | None = None
    # The investigator's model; None means claims go straight to a person.
    agent_model: AgentModel | None = None
    agent_limits: Limits = field(default_factory=Limits)
    # Signs inbound email posts; empty means inbound email is refused.
    inbound_secret: str = ""
    # USD per million tokens (input, output), keyed by model id.
    model_prices: Mapping[str, tuple[float, float]] = field(default_factory=dict)
    # Posts one webhook (url, body, headers) and returns the HTTP status.
    post_webhook: Callable[[str, bytes, dict[str, str]], int] = field(default=post_webhook)
    # Phone calls: both None means the tenant's customers are never called.
    voice: CallPlacer | None = None
    speech: SpeechModel | None = None
    # Numbers that may be called (E.164); "*" allows any. Demo deployments list
    # only numbers whose owners agreed to be called.
    voice_allowed_numbers: frozenset[str] = frozenset()
    voice_max_seconds: int = 420
