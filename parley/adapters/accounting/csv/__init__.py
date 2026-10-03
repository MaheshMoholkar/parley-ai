from typing import Any

from parley.adapters.accounting import AdapterConfigError
from parley.adapters.accounting.csv.adapter import CsvAccountingAdapter, CsvFormatError

__all__ = ["KIND", "CsvAccountingAdapter", "CsvFormatError", "from_config"]

KIND = "csv"


def from_config(config: dict[str, Any]) -> CsvAccountingAdapter:
    """{"kind": "csv", "invoices_path": "...", "payments_path": "..." (optional)}"""
    if not config.get("invoices_path"):
        raise AdapterConfigError("the csv adapter needs invoices_path")
    return CsvAccountingAdapter(config["invoices_path"], config.get("payments_path"))
