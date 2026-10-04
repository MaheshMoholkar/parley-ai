"""The inbound-mail forwarder Lambda, with S3, Secrets Manager and the HTTP
call replaced by fakes."""

import hashlib
import hmac
import io
import json
import urllib.request
from typing import Any

import pytest

from infra.forwarder import handler as forwarder


class Response:
    status = 202

    def __enter__(self) -> "Response":
        return self

    def __exit__(self, *args: object) -> None:
        return None


def test_each_stored_email_is_posted_signed(monkeypatch: pytest.MonkeyPatch) -> None:
    raw = b"From: asha@example.com\r\n\r\nWill pay Friday.\r\n"
    sent: list[urllib.request.Request] = []

    class Clients:
        def get_object(self, Bucket: str, Key: str) -> dict[str, Any]:
            assert (Bucket, Key) == ("inbox", "mail/abc 1")
            return {"Body": io.BytesIO(raw)}

        def get_secret_value(self, SecretId: str) -> dict[str, str]:
            return {"SecretString": json.dumps({"inbound_secret": "s3cret"})}

    monkeypatch.setattr(forwarder.boto3, "client", lambda service: Clients())
    monkeypatch.setattr(forwarder, "_secret", None)
    monkeypatch.setenv("INBOUND_URL", "https://parley.example.in/v1/inbound/email")
    monkeypatch.setenv("APP_SECRET_ARN", "arn:secret")

    def urlopen(request: urllib.request.Request, timeout: float) -> Response:
        sent.append(request)
        return Response()

    monkeypatch.setattr(forwarder.urllib.request, "urlopen", urlopen)
    event = {
        "Records": [
            {"s3": {"bucket": {"name": "inbox"}, "object": {"key": "mail/abc+1"}}},
            # SES's own check that it may write to the bucket: skipped.
            {
                "s3": {
                    "bucket": {"name": "inbox"},
                    "object": {"key": "mail/AMAZON_SES_SETUP_NOTIFICATION"},
                }
            },
        ]
    }

    assert forwarder.handler(event, None) == {"forwarded": 1}
    [request] = sent
    assert request.full_url == "https://parley.example.in/v1/inbound/email"
    assert request.data == raw
    expected = "sha256=" + hmac.new(b"s3cret", raw, hashlib.sha256).hexdigest()
    assert request.get_header("X-parley-signature") == expected


def test_mail_ses_marked_as_spam_or_virus_is_not_forwarded() -> None:
    spam = b"X-SES-Spam-Verdict: FAIL\r\nX-SES-Virus-Verdict: PASS\r\nFrom: x@y\r\n\r\nhi"
    assert forwarder.ses_verdict(spam) == "X-SES-Spam-Verdict: FAIL"
    clean = b"X-SES-Spam-Verdict: PASS\r\nX-SES-Virus-Verdict: PASS\r\nFrom: x@y\r\n\r\nhi"
    assert forwarder.ses_verdict(clean) == ""
    # A sender cannot override SES's verdict with their own header further down.
    forged = b"X-SES-Spam-Verdict: FAIL\r\nX-SES-Spam-Verdict: PASS\r\n\r\nhi"
    assert forwarder.ses_verdict(forged) == "X-SES-Spam-Verdict: FAIL"
