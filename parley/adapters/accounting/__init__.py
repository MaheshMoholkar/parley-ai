"""Accounting adapters, one package each.

Every adapter package exposes:
    KIND         the name a tenant's adapter_config uses, e.g. "csv"
    from_config  builds the adapter from that config (raises AdapterConfigError)

Adapters are found by scanning this folder, so the core never names a
particular source system (the boundary check depends on that).
"""

import importlib
import pkgutil
from collections.abc import Callable
from typing import Any

from parley.ports.accounting import AccountingPort

AdapterFactory = Callable[[dict[str, Any]], AccountingPort]


class AdapterConfigError(ValueError):
    pass


def adapter_factories() -> dict[str, AdapterFactory]:
    factories: dict[str, AdapterFactory] = {}
    for module_info in pkgutil.iter_modules(__path__):
        if not module_info.ispkg:
            continue
        module = importlib.import_module(f"{__name__}.{module_info.name}")
        kind = getattr(module, "KIND", None)
        factory = getattr(module, "from_config", None)
        if isinstance(kind, str) and callable(factory):
            factories[kind] = factory
    return factories
