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
        sender_authenticated=_sender_authenticated(message),
    )


def _sender_authenticated(message: EmailMessage) -> bool:
    """SES adds an Authentication-Results header at the top of each email it
    receives. Only that first header counts: a sender can put fake ones below it."""
    headers = message.get_all("Authentication-Results") or []
    if not headers:
        return False
    first = str(headers[0]).lower()
    if not first.strip().startswith("amazonses.com"):
        return False
    return "dmarc=pass" in first or ("spf=pass" in first and "dkim=pass" in first)


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


# One token per match: a complete tag, a run of text, or a lone "<". Each
# alternative consumes input without backtracking, so this is linear in the
# input whatever a hostile sender puts in it (a regex like "<script.*?</script>"
# is not: it took hours on a few megabytes of unclosed tags).
_TOKENS = re.compile(r"<[^<>]*>|[^<]+|<")
_TAG = re.compile(r"<\s*(/?)\s*([a-zA-Z0-9]+)")
_SKIPPED = frozenset({"script", "style", "blockquote"})
_LINE_BREAKS = frozenset({"br", "p", "div"})


# Inbound mail is untrusted; a reply never needs more text than this.
MAX_HTML_CHARS = 500_000


def _html_to_text(html: str) -> str:
    """Text of an HTML email body, without scripts, styles and quoted earlier
    messages (<blockquote>)."""
    parts: list[str] = []
    skipping = 0  # depth inside skipped elements
    for token in _TOKENS.findall(html[:MAX_HTML_CHARS]):
        tag = _TAG.match(token) if token.endswith(">") else None
        if tag is None:
            if not skipping:
                parts.append(token)
            continue
        closing, name = tag.group(1) == "/", tag.group(2).lower()
        if name in _SKIPPED:
            skipping = max(0, skipping - 1) if closing else skipping + 1
        elif name in _LINE_BREAKS and not skipping and (closing or name == "br"):
            parts.append("\n")
    return unescape("".join(parts))
