"""Reads secrets named in adapter config, so passwords and tokens never sit
in the database, and saves new ones (a tenant's API key) to Secrets Manager.

    "env:ERP_TOKEN_ACME"                 an environment variable
    "aws:arn:aws:secretsmanager:...:x"   an AWS Secrets Manager secret (its string value)
"""

import os

import boto3


class SecretError(ValueError):
    pass


def resolve_secret(reference: str) -> str:
    kind, _, name = reference.partition(":")
    if kind == "env":
        value = os.environ.get(name)
        if not value:
            raise SecretError(f"environment variable {name} is not set")
        return value
    if kind == "aws":
        response = boto3.client("secretsmanager").get_secret_value(SecretId=name)
        return str(response["SecretString"])
    raise SecretError(f"secret references start with env: or aws:, got {reference[:10]!r}...")


def store_secret(name: str, value: str) -> str:
    """Save `value` as an AWS Secrets Manager secret called `name` (created, or
    updated if it exists). Returns the secret's ARN."""
    client = boto3.client("secretsmanager")
    try:
        return str(client.create_secret(Name=name, SecretString=value)["ARN"])
    except client.exceptions.ResourceExistsException:
        return str(client.put_secret_value(SecretId=name, SecretString=value)["ARN"])
