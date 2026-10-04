"""The AWS stack, checked on the CloudFormation template it produces (no AWS
account needed): what is created, what is private, and that no secret is in
plain text."""

import json
from typing import Any

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Match, Template

from infra.config import DeployConfig
from infra.stack import ParleyStack

FULL = {
    "certificate_arn": "arn:aws:acm:ap-south-1:111111111111:certificate/abc",
    "public_url": "https://parley.example.in",
    "model_provider": "bedrock",
    "email_from": "reminders@example.in",
    "reply_domain": "replies.example.in",
    "receive_email": True,
    "speech": True,
    "twilio": True,
    "voice_allowed_numbers": ["+919812345678"],
}


def template(**settings: Any) -> Template:
    app = cdk.App()
    stack = ParleyStack(
        app,
        "Parley",
        config=DeployConfig.from_context(settings),
        env=cdk.Environment(account="111111111111", region="ap-south-1"),
    )
    return Template.from_stack(stack)


@pytest.fixture(scope="module")
def minimal() -> Template:
    return template()


@pytest.fixture(scope="module")
def full() -> Template:
    return template(**FULL)


def containers(t: Template) -> dict[str, dict[str, Any]]:
    found = {}
    for task in t.find_resources("AWS::ECS::TaskDefinition").values():
        for container in task["Properties"]["ContainerDefinitions"]:
            found[container["Name"]] = container
    return found


def test_the_database_is_private_and_encrypted(minimal: Template) -> None:
    minimal.has_resource_properties(
        "AWS::RDS::DBInstance",
        {"Engine": "postgres", "PubliclyAccessible": False, "StorageEncrypted": True},
    )
    minimal.has_resource("AWS::RDS::DBInstance", {"DeletionPolicy": "Snapshot"})


def test_api_worker_and_migrate_run_the_same_image(minimal: Template) -> None:
    found = containers(minimal)
    assert {"api", "worker", "migrate", "otel-collector"} <= set(found)
    assert found["worker"]["Command"] == ["parley", "worker"]
    assert found["migrate"]["Command"] == ["alembic", "upgrade", "head"]
    assert found["api"]["Image"] == found["worker"]["Image"]
    minimal.resource_count_is("AWS::ECS::Service", 2)
    minimal.has_resource_properties(
        "AWS::ElasticLoadBalancingV2::TargetGroup", {"HealthCheckPath": "/healthz"}
    )


def test_secrets_reach_containers_as_ecs_secrets_only(full: Template) -> None:
    for name in ("api", "worker", "migrate"):
        container = containers(full)[name]
        environment = {e["Name"]: e["Value"] for e in container.get("Environment", [])}
        secret_names = {s["Name"] for s in container["Secrets"]}
        assert {
            "PARLEY_DATABASE_SECRET",
            "PARLEY_INBOUND_SECRET",
            "PARLEY_TWILIO_AUTH_TOKEN",
        } <= secret_names
        assert not secret_names & set(environment)
        assert environment["PARLEY_TRACING"] == "otlp"
    # The database password is a reference resolved by CloudFormation, never a value.
    [db] = full.find_resources("AWS::RDS::DBInstance").values()
    assert "resolve:secretsmanager" in json.dumps(db["Properties"]["MasterUserPassword"])


def test_the_task_role_allows_models_email_and_traces(full: Template) -> None:
    actions: set[str] = set()
    for policy in full.find_resources("AWS::IAM::Policy").values():
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]:
            listed = statement["Action"]
            actions |= set(listed if isinstance(listed, list) else [listed])
    assert {
        "bedrock:InvokeModel",
        "bedrock:InvokeModelWithBidirectionalStream",
        "ses:SendEmail",
        "xray:PutTraceSegments",
    } <= actions


def test_https_and_voice_settings(full: Template) -> None:
    full.has_resource_properties(
        "AWS::ElasticLoadBalancingV2::Listener", {"Port": 443, "Protocol": "HTTPS"}
    )
    environment = {e["Name"]: e["Value"] for e in containers(full)["api"]["Environment"]}
    assert environment["PARLEY_VOICE"] == "twilio"
    assert environment["PARLEY_SPEECH_PROVIDER"] == "nova_sonic"
    assert environment["PARLEY_VOICE_ALLOWED_NUMBERS"] == "+919812345678"
    assert environment["PARLEY_PUBLIC_URL"] == "https://parley.example.in"


def test_inbound_mail_goes_through_s3_and_the_forwarder(full: Template, minimal: Template) -> None:
    full.has_resource_properties(
        "AWS::SES::ReceiptRule",
        {"Rule": Match.object_like({"Recipients": ["replies.example.in"]})},
    )
    full.has_resource_properties(
        "AWS::Lambda::Function",
        {
            "Handler": "handler.handler",
            "Environment": {
                "Variables": Match.object_like(
                    {"INBOUND_URL": "https://parley.example.in/v1/inbound/email"}
                )
            },
        },
    )
    minimal.resource_count_is("AWS::SES::ReceiptRule", 0)


@pytest.mark.parametrize(
    ("settings", "problem"),
    [
        ({"twilio": True}, "need speech"),
        ({"speech": True, "twilio": True}, "certificate_arn"),
        (
            {"receive_email": True, "public_url": "https://x", "certificate_arn": "a"},
            "reply_domain",
        ),
        ({"model_provider": "gpt"}, "model_provider"),
        ({"email_from": "reminders@acme.in"}, "reply_domain"),
        ({"email_from": "acme.in", "reply_domain": "r.acme.in"}, "must be an address"),
        ("[1, 2]", "JSON object"),
        ({"surprise": 1}, "unknown"),
    ],
)
def test_bad_settings_are_refused(settings: dict[str, Any] | str, problem: str) -> None:
    with pytest.raises(ValueError, match=problem):
        DeployConfig.from_context(settings)


def test_settings_can_be_given_as_json_on_the_command_line() -> None:
    config = DeployConfig.from_context('{"model_provider": "bedrock", "api_count": 2}')
    assert (config.model_provider, config.api_count) == ("bedrock", 2)


def test_calls_in_progress_outlive_a_deploy(minimal: Template) -> None:
    minimal.has_resource_properties(
        "AWS::ElasticLoadBalancingV2::TargetGroup",
        {
            "TargetGroupAttributes": Match.array_with(
                [{"Key": "deregistration_delay.timeout_seconds", "Value": "480"}]
            )
        },
    )
