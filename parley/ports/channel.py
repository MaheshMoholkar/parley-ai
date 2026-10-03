"""The channel interface: how messages leave and arrive (email in M2, voice later)."""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True)
class OutboundMessage:
    # Stays the same when a send is retried, so a channel can drop duplicates.
    idempotency_key: str
    to_address: str
    subject: str
    body: str
    # Goes into the reply address, so a reply can be matched to this message.
    reply_token: str | None = None


@dataclass(frozen=True)
class InboundMessage:
    """A reply from a customer, whatever the provider delivered it as."""

    provider_message_id: str  # e.g. the email Message-ID; used to drop duplicates
    sender: str
    recipients: tuple[str, ...]
    subject: str
    text: str  # the new text only, with quoted earlier messages removed
    received_at: datetime | None = None


class ChannelPort(Protocol):
    name: str

    def send(self, message: OutboundMessage) -> str:
        """Send the message and return the provider's message id. Raise on failure."""
        ...
