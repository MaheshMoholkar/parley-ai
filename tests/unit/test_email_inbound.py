from parley.adapters.channels.email_inbound import parse_email, reply_token, strip_quoted_text

PLAIN = b"""From: Asha Stores <Asha@Example.com>
To: reply+3f9ca1b2c3d4e5f60718293a4b5c6d7e@replies.example.com
Subject: Re: Invoice A-1 is overdue
Message-ID: <abc123@mail.example.com>
Date: Mon, 05 Jan 2026 12:00:00 +0530
Content-Type: text/plain; charset=utf-8

Will pay parso, sorry for the delay.

On Mon, 5 Jan 2026 at 10:00, Acme Traders <reminders@example.com> wrote:
> This is a friendly reminder that the following invoice is now overdue.
> - Invoice A-1: INR 1,234.50
"""

HTML_ONLY = b"""From: bala@example.com
To: reply+00112233445566778899aabbccddeeff@replies.example.com
Subject: Re: reminder
Content-Type: text/html; charset=utf-8

<div>We already paid on 2 Jan.<br>UTR 1234</div><blockquote>old text</blockquote>
"""


def test_plain_reply_is_parsed_and_quote_removed() -> None:
    email = parse_email(PLAIN)
    assert email.sender == "asha@example.com"
    assert email.provider_message_id == "<abc123@mail.example.com>"
    assert email.subject == "Re: Invoice A-1 is overdue"
    assert email.text == "Will pay parso, sorry for the delay."
    assert email.received_at is not None
    assert reply_token(email.recipients) == "3f9ca1b2c3d4e5f60718293a4b5c6d7e"


def test_html_only_reply_becomes_text() -> None:
    email = parse_email(HTML_ONLY)
    assert email.text == "We already paid on 2 Jan.\nUTR 1234"


def test_no_token_without_a_reply_address() -> None:
    assert reply_token(("accounts@acme.example",)) is None
    assert reply_token(("reply+not-hex@replies.example.com",)) is None


def test_outlook_style_quotes_are_removed() -> None:
    text = "Paying Friday.\n\n-----Original Message-----\nFrom: Acme\nold"
    assert strip_quoted_text(text) == "Paying Friday."
