"""Picks the accounting adapter a tenant is configured to use."""

from parley.adapters.accounting import AdapterConfigError, adapter_factories
from parley.db.models import Tenant
from parley.ports.accounting import AccountingPort

__all__ = ["AdapterConfigError", "accounting_adapter_for"]


def accounting_adapter_for(tenant: Tenant) -> AccountingPort:
    kind = tenant.adapter_config.get("kind")
    factory = adapter_factories().get(str(kind))
    if factory is None:
        raise AdapterConfigError(f"tenant {tenant.id}: unknown accounting adapter {kind!r}")
    try:
        return factory(tenant.adapter_config)
    except AdapterConfigError as exc:
        raise AdapterConfigError(f"tenant {tenant.id}: {exc}") from None
