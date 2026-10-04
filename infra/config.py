"""Deployment settings, read from the CDK context ("parley" in cdk.json, or
`-c parley='{...}'` on the command line).

Everything is optional except where noted: the smallest deployment runs the
API and worker with the fixed reminder template, no email sending and no calls.
"""

import json
from dataclasses import dataclass, field, fields
from typing import Any


@dataclass(frozen=True)
class DeployConfig:
    # HTTPS for the load balancer (an ACM certificate in the stack's region).
    # Needed for Twilio and for anything but a test; without it the API is
    # served over plain HTTP.
    certificate_arn: str = ""
    # The public https address the certificate is for, e.g. https://parley.acme.in.
    # Twilio and the inbound-mail forwarder call it.
    public_url: str = ""

    # "bedrock" drafts with Claude and reads replies; "template" uses no model.
    model_provider: str = "template"

    # Sending email with SES from this address (its domain must be verified in SES).
    email_from: str = ""
    # Replies go to reply+<token>@<reply_domain>; needed whenever email is sent.
    # With receive_email, SES stores mail for this domain in S3 and a Lambda
    # posts it to the API (SES receives mail only in some regions).
    reply_domain: str = ""
    receive_email: bool = False

    # Voice: Nova 2 Sonic for the agent's speech, Twilio for phone calls.
    speech: bool = False
    twilio: bool = False
    # Secrets Manager secret holding {"account_sid", "auth_token", "from_number"},
    # created by hand (it is never in the code or the template).
    twilio_secret_name: str = "parley/twilio"
    voice_allowed_numbers: list[str] = field(default_factory=list)

    # Sizes.
    db_instance_type: str = "t4g.micro"
    api_count: int = 1
    # Keep the database when the stack is deleted (turn off only for experiments).
    keep_database: bool = True

    @classmethod
    def from_context(cls, raw: dict[str, Any] | str | None) -> "DeployConfig":
        # From cdk.json the settings arrive as an object; from `-c parley='{...}'`
        # on the command line, as a JSON string.
        if isinstance(raw, str):
            raw = json.loads(raw)
        raw = raw or {}
        if not isinstance(raw, dict):
            raise ValueError("the parley settings must be a JSON object")
        known = {f.name for f in fields(cls)}
        unknown = set(raw) - known
        if unknown:
            raise ValueError(f"unknown parley settings: {', '.join(sorted(unknown))}")
        config = cls(**raw)
        config.check()
        return config

    def check(self) -> None:
        if self.model_provider not in ("template", "bedrock"):
            raise ValueError("model_provider must be 'template' or 'bedrock'")
        if self.twilio and not self.speech:
            raise ValueError("twilio calls need speech: true")
        if (self.twilio or self.receive_email) and not (
            self.public_url.startswith("https://") and self.certificate_arn
        ):
            raise ValueError(
                "twilio and receive_email need certificate_arn and an https public_url"
            )
        if self.email_from:
            local, _, domain = self.email_from.partition("@")
            if not local or "." not in domain:
                raise ValueError(f"email_from must be an address, got {self.email_from!r}")
            if not self.reply_domain:
                # Otherwise replies would go to the app's placeholder domain and be lost.
                raise ValueError("email_from needs reply_domain, where customers' replies go")
        if self.receive_email and not self.reply_domain:
            raise ValueError("receive_email needs reply_domain")
