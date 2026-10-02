"""The things every service needs, bundled so tests can swap any of them.

Production builds a Runtime with the real clock and channel; tests build one
with a FakeClock and a DryRunChannel.
"""

from collections.abc import Callable
from dataclasses import dataclass, field

from parley.db.models import Tenant
from parley.db.session import SessionFactory
from parley.ports.accounting import AccountingPort
from parley.ports.channel import ChannelPort
from parley.ports.clock import Clock
from parley.services.accounting import accounting_adapter_for


@dataclass
class Runtime:
    session_factory: SessionFactory
    clock: Clock
    channel: ChannelPort
    accounting_for: Callable[[Tenant], AccountingPort] = field(default=accounting_adapter_for)
