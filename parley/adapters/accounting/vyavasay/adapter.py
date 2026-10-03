"""The Vyavasay ERP as an accounting source (spec: "Vyavasay adapter").

What Vyavasay gives, and how it is turned into what the core expects:

- Open invoices: posted sales invoices whose payment status is unpaid, partial
  or overdue. There is no "changed since" filter, so the full list is read
  each sync (the core needs the full list anyway).
- Amount due: Vyavasay's balance (total minus applied payments), minus any open
  balance of posted credit notes linked to the invoice. Vyavasay only counts a
  credit note once someone records an offset, so without this the core would
  chase money the customer has already been credited.
- Due date: an invoice can have none; then the invoice date is used and the
  reminder says so.
- Status: "cancelled" is void; "paid" is paid; anything else with a balance is open.
- Payments: posted incoming payments. They carry no currency; Vyavasay
  invoices are always in the tenant's base currency (INR).
- Contacts: one email and one phone per party.

Money arrives as decimal strings and dates as YYYY-MM-DD or RFC 3339; both are
converted here, so nothing Vyavasay-shaped reaches the core.
"""

from collections import defaultdict
from datetime import date
from typing import Any

from parley.adapters.accounting.vyavasay.client import VyavasayClient
from parley.core.domain import InvoiceStatus
from parley.core.money import to_minor_units
from parley.ports.accounting import SourceCustomer, SourceInvoice, SourcePayment

OPEN_PAYMENT_STATUSES = ("unpaid", "partial", "overdue")
OPEN_CREDIT_STATUSES = ("open", "partial")


class VyavasayAccountingAdapter:
    source = "vyavasay"

    def __init__(self, client: VyavasayClient, currency: str = "INR") -> None:
        self.client = client
        self.currency = currency
        self._credits: dict[str, int] | None = None

    # --- AccountingPort methods ----------------------------------------------------

    def list_open_invoices(self) -> list[SourceInvoice]:
        params = [("status", "posted"), *(("paymentStatus", s) for s in OPEN_PAYMENT_STATUSES)]
        invoices = []
        for item in self.client.get_all("/v1/sales-invoices", params):
            invoice = self._invoice(item["invoice"], item["balanceAmount"], item["paymentStatus"])
            if invoice.status == InvoiceStatus.OPEN:
                invoices.append(invoice)
        return invoices

    def get_invoice(self, external_id: str) -> SourceInvoice | None:
        detail = self.client.get(f"/v1/sales-invoices/{external_id}")
        if detail is None:
            return None
        balance = detail["balance"]
        return self._invoice(detail["invoice"], balance["balanceAmount"], balance["paymentStatus"])

    def get_customer(self, external_id: str) -> SourceCustomer | None:
        party = self.client.get(f"/v1/parties/{external_id}")
        if party is None:
            return None
        return SourceCustomer(
            external_id=str(party["id"]),
            name=party.get("displayName") or party["name"],
            email=party.get("email") or None,
            phone=party.get("phone") or None,
        )

    def list_payments_since(
        self, since: date, customer_external_id: str | None = None
    ) -> list[SourcePayment]:
        params: list[tuple[str, Any]] = [
            ("direction", "in"),
            ("status", "posted"),
            ("fromDate", since.isoformat()),
        ]
        if customer_external_id:
            params.append(("partyId", customer_external_id))
        return [
            SourcePayment(
                external_id=str(p["id"]),
                customer_external_id=str(p["partyId"]),
                amount=to_minor_units(p["amount"], self.currency),
                currency=self.currency,
                paid_on=_date(p["paymentDate"]),
                reference=p.get("reference") or p.get("paymentNumber") or "",
            )
            for p in self.client.get_all("/v1/payments", params)
            # A cheque counts once it has cleared.
            if p.get("chequeStatus") in (None, "cleared")
        ]

    # --- Mapping -------------------------------------------------------------------------

    def _invoice(self, raw: dict[str, Any], balance: str, payment_status: str) -> SourceInvoice:
        currency = raw.get("currencyCode") or self.currency
        amount_due = to_minor_units(balance, currency)
        credit = self._open_credits().get(str(raw["id"]), 0)
        details = []
        if credit:
            amount_due = max(amount_due - credit, 0)
            details.append("after an unapplied credit note")
        if raw.get("dueDate"):
            due = _date(raw["dueDate"])
        else:
            due = _date(raw["invoiceDate"])
            details.append("no due date on the invoice; the invoice date is used")

        if raw.get("status") == "cancelled" or payment_status == "cancelled":
            status = InvoiceStatus.VOID
        elif payment_status == "paid" or amount_due == 0:
            status = InvoiceStatus.PAID
        else:
            status = InvoiceStatus.OPEN

        return SourceInvoice(
            external_id=str(raw["id"]),
            customer_external_id=str(raw["customerId"]),
            number=raw["invoiceNumber"],
            amount_due=amount_due,
            currency=currency,
            due_date=due,
            status=status,
            display_details="; ".join(details),
        )

    def _open_credits(self) -> dict[str, int]:
        """Open balance of posted credit notes, by the invoice they belong to.
        Read once per adapter instance (one sync)."""
        if self._credits is None:
            credits: dict[str, int] = defaultdict(int)
            for status in OPEN_CREDIT_STATUSES:
                params = [("status", "posted"), ("applicationStatus", status)]
                for note in self.client.get_all("/v1/sale-returns", params):
                    if note.get("salesInvoiceId"):
                        credits[str(note["salesInvoiceId"])] += to_minor_units(
                            note["balanceAmount"], note.get("currencyCode") or self.currency
                        )
            self._credits = dict(credits)
        return self._credits


def _date(text: str) -> date:
    """Vyavasay sends dates as "2026-01-01" or "2026-01-01T00:00:00Z"."""
    return date.fromisoformat(text[:10])
