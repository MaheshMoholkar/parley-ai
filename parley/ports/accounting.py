"""The accounting interface (spec: "Accounting interface").

Every source system (CSV files, an ERP, ...) is wrapped in an adapter with
these methods. The rest of the app calls nothing else, so swapping the source
never touches the core.

`Protocol` means "any class with these methods counts"; adapters do not need to
inherit from it.
"""

from dataclasses import dataclass
from datetime import date
from typing import Protocol, runtime_checkable

from parley.core.domain import InvoiceStatus


@dataclass(frozen=True)
class SourceCustomer:
    external_id: str
    name: str
    email: str | None
    phone: str | None


@dataclass(frozen=True)
class SourceInvoice:
    """An invoice as the source reports it.

    Adapters guarantee: `amount_due` is in minor units and already net of payments
    and applied credit notes, and `due_date` is always set.
    """

    external_id: str
    customer_external_id: str
    number: str
    amount_due: int
    currency: str
    due_date: date
    status: InvoiceStatus
    display_details: str = ""


@dataclass(frozen=True)
class SourcePayment:
    external_id: str
    customer_external_id: str
    amount: int
    currency: str
    paid_on: date
    reference: str = ""


class AccountingPort(Protocol):
    # Short name stored on every synced row, e.g. "csv".
    source: str

    def list_open_invoices(self) -> list[SourceInvoice]:
        """Every invoice that is open in the source right now (the complete list)."""
        ...

    def get_invoice(self, external_id: str) -> SourceInvoice | None:
        """One invoice in any status, or None if the source no longer has it."""
        ...

    def get_customer(self, external_id: str) -> SourceCustomer | None: ...

    def list_payments_since(
        self, since: date, customer_external_id: str | None = None
    ) -> list[SourcePayment]: ...


# --- Write-back (optional) ------------------------------------------------------------


class NoteError(RuntimeError):
    """A note could not be written. `retryable` is False when trying again cannot
    help (the source refused it, or does not support notes); True when the source
    was down or busy."""

    def __init__(self, message: str, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


@runtime_checkable
class NoteWriter(Protocol):
    """An adapter that can show collection activity in the source system
    (spec: "Write-back"). Optional: an adapter without `add_note` simply does not
    write back, and the core never asks it to."""

    def add_note(self, invoice_external_id: str, text: str, idempotency_key: str) -> None:
        """Add an internal note to the invoice. `idempotency_key` stays the same
        when a note is retried, so the source can drop a duplicate. Raises NoteError."""
        ...
