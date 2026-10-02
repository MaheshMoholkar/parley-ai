"""A channel that records messages instead of sending them (M1, demos and tests)."""

import logging

from parley.ports.channel import OutboundMessage

log = logging.getLogger(__name__)


class DryRunChannel:
    name = "dry_run"

    def __init__(self) -> None:
        # Keyed by idempotency key, so a retried send is stored once.
        self.sent: dict[str, OutboundMessage] = {}

    def send(self, message: OutboundMessage) -> str:
        if message.idempotency_key not in self.sent:
            self.sent[message.idempotency_key] = message
            log.info("dry-run send to %s: %s", message.to_address, message.subject)
        return f"dry-run:{message.idempotency_key}"
