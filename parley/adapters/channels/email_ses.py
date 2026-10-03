"""Sends email through Amazon SES (API v2).

Each message gets a Reply-To address carrying its reply token, for example
reply+3f9c...@replies.example.com. SES receiving on that domain hands replies
back to the service (see parley/adapters/channels/email_inbound.py).
"""

from typing import Any

import boto3

from parley.ports.channel import OutboundMessage


class SesEmailChannel:
    name = "email"

    def __init__(
        self, from_address: str, reply_domain: str, region: str, client: Any = None
    ) -> None:
        self.from_address = from_address
        self.reply_domain = reply_domain
        self._client = client or boto3.client("sesv2", region_name=region)

    def send(self, message: OutboundMessage) -> str:
        content: dict[str, Any] = {
            "Simple": {
                "Subject": {"Data": message.subject, "Charset": "UTF-8"},
                "Body": {"Text": {"Data": message.body, "Charset": "UTF-8"}},
                # SES does not drop duplicates itself; the header lets anyone
                # tracing a duplicate see that both copies came from one send.
                "Headers": [{"Name": "X-Parley-Idempotency-Key", "Value": message.idempotency_key}],
            }
        }
        request: dict[str, Any] = {
            "FromEmailAddress": self.from_address,
            "Destination": {"ToAddresses": [message.to_address]},
            "Content": content,
        }
        if message.reply_token:
            request["ReplyToAddresses"] = [reply_address(message.reply_token, self.reply_domain)]
        # Errors (throttling, bad address) raise, and the delivery step retries.
        response = self._client.send_email(**request)
        return str(response["MessageId"])


def reply_address(token: str, domain: str) -> str:
    return f"reply+{token}@{domain}"
