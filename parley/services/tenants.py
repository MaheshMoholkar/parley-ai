"""Creating tenants and finding a tenant from its API key."""

import hashlib
import secrets
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.orm import Session

from parley.core.policy import Policy
from parley.db.models import Tenant

API_KEY_PREFIX = "pk_"


def hash_api_key(api_key: str) -> str:
    # API keys are long random strings, so a plain SHA-256 is enough; slow
    # password hashes are only needed for short, guessable passwords.
    return hashlib.sha256(api_key.encode()).hexdigest()


def create_tenant(
    session: Session,
    name: str,
    timezone: str,
    adapter_config: dict[str, Any],
    policy_overrides: dict[str, Any] | None = None,
) -> tuple[Tenant, str]:
    """Create a tenant and return it with its API key. The key is not stored and
    cannot be shown again."""
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError(f"unknown timezone {timezone!r}") from None
    overrides = policy_overrides or {}
    Policy.model_validate(overrides)  # fail now, not on the first worker run

    api_key = API_KEY_PREFIX + secrets.token_urlsafe(32)
    tenant = Tenant(
        name=name,
        timezone=timezone,
        adapter_config=adapter_config,
        policy_overrides=overrides,
        api_key_hash=hash_api_key(api_key),
    )
    session.add(tenant)
    session.flush()  # assigns the id
    return tenant, api_key


def find_tenant_by_api_key(session: Session, api_key: str) -> Tenant | None:
    return session.scalar(select(Tenant).where(Tenant.api_key_hash == hash_api_key(api_key)))
