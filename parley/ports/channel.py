"""The channel interface: how a message leaves the service (email in M2, voice later)."""

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class OutboundMessage:
    # Stays the same when a send is retried, so a channel can drop duplicates.
    idempotency_key: str
    to_address: str
    subject: str
    body: str


class ChannelPort(Protocol):
    name: str

    def send(self, message: OutboundMessage) -> str:
        """Send the message and return the provider's message id. Raise on failure."""
        ...
