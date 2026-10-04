"""Forwards inbound email to parley (runs as an AWS Lambda function).

SES stores each email for the reply domain in S3; the new object triggers this
function, which posts the raw email to POST /v1/inbound/email with the header
X-Parley-Signature: sha256=<HMAC-SHA256 of the body with the inbound secret>.

If parley does not accept the email, the function raises, so Lambda retries it
and finally puts the event on the dead-letter queue, where a person can see it.
Uses only the standard library and boto3, which the Lambda runtime provides.
"""

import hashlib
import hmac
import json
import os
import urllib.request
from email.parser import BytesHeaderParser
from typing import Any
from urllib.parse import unquote_plus

import boto3

# SES writes this object once, to check it may write to the bucket.
SES_SETUP_OBJECT = "AMAZON_SES_SETUP_NOTIFICATION"

_secret: str | None = None


def handler(event: dict[str, Any], context: Any) -> dict[str, int]:
    s3 = boto3.client("s3")
    forwarded = 0
    for record in event["Records"]:
        bucket = record["s3"]["bucket"]["name"]
        key = unquote_plus(record["s3"]["object"]["key"])
        if key.endswith(SES_SETUP_OBJECT):
            continue  # SES's test write when the rule is created; not an email
        raw = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
        verdict = ses_verdict(raw)
        if verdict:
            print(f"not forwarding {key}: {verdict}")  # goes to CloudWatch Logs
            continue
        forward(raw, os.environ["INBOUND_URL"], inbound_secret())
        forwarded += 1
    return {"forwarded": forwarded}


def ses_verdict(raw: bytes) -> str:
    """Why SES says not to trust this email ("" if it passed). SES adds its
    verdict headers at the top; a sender's own copies further down do not count."""
    headers = BytesHeaderParser().parsebytes(raw)
    for name in ("X-SES-Virus-Verdict", "X-SES-Spam-Verdict"):
        values = headers.get_all(name) or []
        if values and str(values[0]).strip().upper() == "FAIL":
            return f"{name}: FAIL"
    return ""


def forward(raw: bytes, url: str, secret: str) -> None:
    signature = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    request = urllib.request.Request(
        url,
        data=raw,
        method="POST",
        headers={"Content-Type": "message/rfc822", "X-Parley-Signature": signature},
    )
    with urllib.request.urlopen(request, timeout=20) as response:  # raises on 4xx/5xx
        if response.status >= 300:
            raise RuntimeError(f"parley answered {response.status}")


def inbound_secret() -> str:
    """Read once per Lambda container from Secrets Manager."""
    global _secret
    if _secret is None:
        value = boto3.client("secretsmanager").get_secret_value(
            SecretId=os.environ["APP_SECRET_ARN"]
        )
        _secret = str(json.loads(value["SecretString"])["inbound_secret"])
    return _secret
