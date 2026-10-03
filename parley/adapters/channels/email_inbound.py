"""Turns a raw inbound email (MIME) into an InboundMessage.

The service receives raw email bytes, for example from SES receiving via a small
forwarder. Only the customer's new text is kept: quoted earlier messages are
removed, so the reply reader sees what the customer actually wrote.
"""

import re
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import getaddresses, parseaddr, parsedate_to_datetime
from html import unescape

from parley.ports.channel import InboundMessage

_TOKEN = re.compile(r"^reply\+([0-9a-f]{16,64})@", re.I)

# Lines that start the quoted part of a reply in common mail clients.
_QUOTE_HEADERS = [
    re.compile(r"^On .+ wrote:\s*$"),  # Gmail, Apple Mail
    re.compile(r"^-{2,}\s*Original Message\s*-{2,}", re.I),  # Outlook
    re.compile(r"^From: .+$"),  # Outlook, after a separator line
    re.compile(r"^_{5,}\s*$"),
]


def parse_email(raw: bytes) -> InboundMessage:
    message = BytesParser(policy=policy.default).parsebytes(raw)
    assert isinstance(message, EmailMessage)
    recipients = tuple(
        address.lower()
        for _, address in getaddresses(
            message.get_all("To", [])
            + message.get_all("Cc", [])
            + message.get_all("Delivered-To", [])
        )
        if address
    )
    received_at = None
    if message["Date"]:
        try:
            received_at = parsedate_to_datetime(str(message["Date"]))
        except (TypeError, ValueError):
            received_at = None
    return InboundMessage(
        provider_message_id=str(message["Message-ID"] or "").strip(),
        sender=parseaddr(str(message["From"] or ""))[1].lower(),
        recipients=recipients,
        subject=str(message["Subject"] or ""),
        text=strip_quoted_text(_body_text(message)),
        received_at=received_at,
    )


def reply_token(recipients: tuple[str, ...]) -> str | None:
    """The reply token from the first reply+<token>@... recipient, if any."""
    for address in recipients:
        match = _TOKEN.match(address)
        if match:
            return match.group(1).lower()
    return None


def strip_quoted_text(text: str) -> str:
    kept = []
    for line in text.splitlines():
        if any(pattern.match(line.strip()) for pattern in _QUOTE_HEADERS):
            break
        if line.lstrip().startswith(">"):
            continue
        kept.append(line)
    return "\n".join(kept).strip()


def _body_text(message: EmailMessage) -> str:
    part = message.get_body(preferencelist=("plain", "html"))
    if part is None:
        return ""
    content = part.get_content()
    if part.get_content_subtype() == "html":
        content = _html_to_text(str(content))
    return str(content)


def _html_to_text(html: str) -> str:
    html = re.sub(r"(?is)<(script|style).*?</\1>", "", html)
    html = re.sub(r"(?i)<br\s*/?>|</p>|</div>", "\n", html)
    html = re.sub(r"(?i)<blockquote.*?</blockquote>", "", html, flags=re.S)
    return unescape(re.sub(r"<[^>]+>", "", html))
