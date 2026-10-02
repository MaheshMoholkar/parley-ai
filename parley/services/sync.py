"""Sync: copy customers, invoices and payments from the source system into the core.

One sync is one transaction, so a failure part-way leaves the previous data in
place. The tenant row is locked for the duration, so two syncs of the same tenant
never overlap.

Steps:
1. Fetch the complete list of open invoices and upsert them (and their customers).
2. Any invoice we still hold as open that the source no longer lists is looked
   up directly; if the source does not return it, it is marked `removed`.
3. Upsert payments recorded since shortly before the last sync.
4. Tell each affected case what changed (this closes or reopens cases).
5. Open cases for invoices that are now overdue.
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from parley.core.domain import CaseState, InvoiceStatus
from parley.core.workflow import on_source_update
from parley.db.models import Case, Customer, Invoice, Payment, Tenant
from parley.ports.accounting import AccountingPort, SourceInvoice, SourcePayment
from parley.services.cases import apply_transition, open_overdue_cases
from parley.services.runtime import Runtime

log = logging.getLogger(__name__)

# Payments can be entered in the source a few days after the payment date, so
# each sync looks back this far before the previous sync.
PAYMENT_LOOKBACK = timedelta(days=30)


class SyncError(RuntimeError):
    pass


@dataclass
class SyncResult:
    invoices_seen: int = 0
    invoices_created: int = 0
    invoices_updated: int = 0
    invoices_no_longer_open: int = 0
    payments_created: int = 0
    cases_opened: int = 0
    cases_closed: int = 0
    cases_reopened: int = 0


def sync_tenant(rt: Runtime, tenant_id: uuid.UUID) -> SyncResult:
    now = rt.clock.now()
    result = SyncResult()
    with rt.session_factory.begin() as session:
        tenant = session.get_one(Tenant, tenant_id, with_for_update=True)
        accounting = rt.accounting_for(tenant)
        customers = _Customers(session, tenant, accounting)

        open_invoices = accounting.list_open_invoices()
        result.invoices_seen = len(open_invoices)
        touched = _upsert_open_invoices(
            session, tenant, accounting, customers, open_invoices, result
        )
        touched += _refresh_unlisted_invoices(session, tenant, accounting, open_invoices, result)
        _upsert_payments(session, tenant, accounting, customers, result)

        _update_cases(session, touched, now, result)
        result.cases_opened = open_overdue_cases(session, tenant, now)
        tenant.last_synced_at = now

    log.info("synced tenant %s: %s", tenant_id, result)
    return result


class _Customers:
    """Finds or creates the core's customer row for a source customer id,
    fetching each customer from the source at most once per sync."""

    def __init__(self, session: Session, tenant: Tenant, accounting: AccountingPort) -> None:
        self.session = session
        self.tenant = tenant
        self.accounting = accounting
        self._by_external_id: dict[str, Customer | None] = {}

    def get(self, external_id: str) -> Customer | None:
        if external_id not in self._by_external_id:
            self._by_external_id[external_id] = self._sync_one(external_id)
        return self._by_external_id[external_id]

    def _sync_one(self, external_id: str) -> Customer | None:
        source_customer = self.accounting.get_customer(external_id)
        if source_customer is None:
            return None
        customer = self.session.scalar(
            select(Customer).where(
                Customer.tenant_id == self.tenant.id,
                Customer.source == self.accounting.source,
                Customer.external_id == external_id,
            )
        )
        if customer is None:
            customer = Customer(
                id=uuid.uuid4(),
                tenant_id=self.tenant.id,
                source=self.accounting.source,
                external_id=external_id,
                brief="",
                paused=False,
            )
            self.session.add(customer)
        # Source-owned fields are overwritten; core-owned ones (brief, paused,
        # language) are left alone.
        customer.name = source_customer.name
        customer.email = source_customer.email
        customer.phone = source_customer.phone
        return customer


def _upsert_open_invoices(
    session: Session,
    tenant: Tenant,
    accounting: AccountingPort,
    customers: _Customers,
    open_invoices: list[SourceInvoice],
    result: SyncResult,
) -> list[Invoice]:
    existing = {
        invoice.external_id: invoice
        for invoice in session.scalars(
            select(Invoice).where(
                Invoice.tenant_id == tenant.id,
                Invoice.source == accounting.source,
                Invoice.external_id.in_([i.external_id for i in open_invoices]),
            )
        )
    }
    touched = []
    for source_invoice in open_invoices:
        customer = customers.get(source_invoice.customer_external_id)
        if customer is None:
            raise SyncError(
                f"invoice {source_invoice.number} names customer "
                f"{source_invoice.customer_external_id!r}, which the source does not return"
            )
        invoice = existing.get(source_invoice.external_id)
        if invoice is None:
            invoice = Invoice(
                id=uuid.uuid4(),
                tenant_id=tenant.id,
                source=accounting.source,
                external_id=source_invoice.external_id,
            )
            session.add(invoice)
            result.invoices_created += 1
        elif _differs(invoice, source_invoice, customer):
            result.invoices_updated += 1
        _copy_invoice(invoice, source_invoice, customer)
        touched.append(invoice)
    return touched


def _refresh_unlisted_invoices(
    session: Session,
    tenant: Tenant,
    accounting: AccountingPort,
    open_invoices: list[SourceInvoice],
    result: SyncResult,
) -> list[Invoice]:
    """Invoices we hold as open but the source no longer lists as open."""
    listed = {i.external_id for i in open_invoices}
    held_open = session.scalars(
        select(Invoice).where(
            Invoice.tenant_id == tenant.id,
            Invoice.source == accounting.source,
            Invoice.status == InvoiceStatus.OPEN,
        )
    ).all()

    touched = []
    for invoice in held_open:
        if invoice.external_id in listed:
            continue
        latest = accounting.get_invoice(invoice.external_id)
        if latest is None:
            invoice.status = InvoiceStatus.REMOVED
        else:
            invoice.status = latest.status
            invoice.amount_due = latest.amount_due
            invoice.due_date = latest.due_date
        result.invoices_no_longer_open += 1
        touched.append(invoice)
    return touched


def _differs(invoice: Invoice, source: SourceInvoice, customer: Customer) -> bool:
    return (
        invoice.customer_id != customer.id
        or invoice.amount_due != source.amount_due
        or invoice.due_date != source.due_date
        or invoice.status != source.status
        or invoice.number != source.number
        or invoice.currency != source.currency
        or invoice.display_details != source.display_details
    )


def _copy_invoice(invoice: Invoice, source: SourceInvoice, customer: Customer) -> None:
    invoice.customer_id = customer.id
    invoice.number = source.number
    invoice.amount_due = source.amount_due
    invoice.currency = source.currency
    invoice.due_date = source.due_date
    invoice.status = source.status
    invoice.display_details = source.display_details


def _upsert_payments(
    session: Session,
    tenant: Tenant,
    accounting: AccountingPort,
    customers: _Customers,
    result: SyncResult,
) -> None:
    since = date.min
    if tenant.last_synced_at is not None:
        since = tenant.last_synced_at.astimezone(tenant.zone).date() - PAYMENT_LOOKBACK

    source_payments = accounting.list_payments_since(since)
    existing = {
        payment.external_id: payment
        for payment in session.scalars(
            select(Payment).where(
                Payment.tenant_id == tenant.id,
                Payment.source == accounting.source,
                Payment.external_id.in_([p.external_id for p in source_payments]),
            )
        )
    }
    for source_payment in source_payments:
        customer = customers.get(source_payment.customer_external_id)
        if customer is None:
            log.warning(
                "skipping payment %s: unknown customer %r",
                source_payment.external_id,
                source_payment.customer_external_id,
            )
            continue
        payment = existing.get(source_payment.external_id)
        if payment is None:
            payment = Payment(
                id=uuid.uuid4(),
                tenant_id=tenant.id,
                source=accounting.source,
                external_id=source_payment.external_id,
            )
            session.add(payment)
            result.payments_created += 1
        _copy_payment(payment, source_payment, customer)


def _copy_payment(payment: Payment, source: SourcePayment, customer: Customer) -> None:
    payment.customer_id = customer.id
    payment.amount = source.amount
    payment.currency = source.currency
    payment.paid_on = source.paid_on
    payment.reference = source.reference


def _update_cases(
    session: Session, invoices: list[Invoice], now: datetime, result: SyncResult
) -> None:
    by_id = {invoice.id: invoice for invoice in invoices}
    if not by_id:
        return
    session.flush()  # make sure new and changed invoices are in the database first
    cases = session.scalars(select(Case).where(Case.invoice_id.in_(by_id)).with_for_update()).all()
    for case in cases:
        invoice = by_id[case.invoice_id]
        case.customer_id = invoice.customer_id
        transition = on_source_update(case.view(), invoice.status, invoice.amount_due, now)
        if transition is None:
            continue
        if transition.state == CaseState.CLOSED:
            result.cases_closed += 1
        else:
            result.cases_reopened += 1
        apply_transition(session, case, transition, now)
