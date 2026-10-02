"""Picks the accounting adapter a tenant is configured to use."""

from parley.adapters.accounting.csv import CsvAccountingAdapter
from parley.db.models import Tenant
from parley.ports.accounting import AccountingPort


class AdapterConfigError(ValueError):
    pass


def accounting_adapter_for(tenant: Tenant) -> AccountingPort:
    config = tenant.adapter_config
    kind = config.get("kind")
    if kind == "csv":
        if not config.get("invoices_path"):
            raise AdapterConfigError(f"tenant {tenant.id}: csv adapter needs invoices_path")
        return CsvAccountingAdapter(config["invoices_path"], config.get("payments_path"))
    raise AdapterConfigError(f"tenant {tenant.id}: unknown accounting adapter {kind!r}")
