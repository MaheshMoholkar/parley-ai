"""Vyavasay ERP adapter. All Vyavasay-specific code lives in this folder.

Tenant adapter_config:

    {
      "kind": "vyavasay",
      "base_url": "https://api.example-vyavasay.in",
      // either an API token...
      "token": "env:VYAVASAY_TOKEN_ACME",
      // ...or a dedicated Vyavasay user with view permissions on every location
      "phone": "98xxxxxxxx",
      "password": "aws:arn:aws:secretsmanager:ap-south-1:...:secret:vyavasay-acme",
      "vyavasay_tenant_id": "<the tenant's id in Vyavasay>",
      "currency": "INR",
      // add notes about collection activity to the invoices (off by default;
      // needs Vyavasay's collection-activity endpoint, see adapter.py; never
      // the invoice's own notes field, which is printed on the invoice)
      "write_notes": true
    }

Secrets are references ("env:NAME" or "aws:<secret arn>"), resolved when the
adapter is built, never stored in the database.
"""

from typing import Any

from parley.adapters.accounting import AdapterConfigError
from parley.adapters.accounting.vyavasay.adapter import VyavasayAccountingAdapter
from parley.adapters.accounting.vyavasay.client import VyavasayClient, VyavasayError
from parley.adapters.secrets import SecretError, resolve_secret

__all__ = ["KIND", "VyavasayAccountingAdapter", "VyavasayClient", "from_config"]

KIND = "vyavasay"


def from_config(config: dict[str, Any]) -> VyavasayAccountingAdapter:
    if not config.get("base_url"):
        raise AdapterConfigError("the vyavasay adapter needs base_url")
    try:
        token = resolve_secret(config["token"]) if config.get("token") else None
        password = resolve_secret(config["password"]) if config.get("password") else None
        client = VyavasayClient(
            config["base_url"],
            token=token,
            phone=config.get("phone"),
            password=password,
            vyavasay_tenant_id=config.get("vyavasay_tenant_id"),
        )
    except (SecretError, VyavasayError) as exc:
        raise AdapterConfigError(str(exc)) from None
    return VyavasayAccountingAdapter(client, currency=config.get("currency", "INR"))
