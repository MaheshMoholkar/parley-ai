from pathlib import Path

import pytest
from sqlalchemy import func, select

from parley.adapters.accounting.csv import CsvFormatError
from parley.core.domain import CaseState, CloseReason, InvoiceStatus, TaskKind, TaskStatus
from parley.db.models import Case, Customer, Invoice, Task
from parley.services.runtime import Runtime
from parley.services.sync import sync_tenant
from tests.integration.conftest import invoice_row, make_tenant, write_aging

# The test clock starts on 2026-01-05.
OVERDUE = invoice_row("INV-1", "Asha", "1000", "2026-01-01")
NOT_DUE = invoice_row("INV-2", "Asha", "500", "2026-01-20")


def case_for(rt: Runtime, number: str) -> Case | None:
    with rt.session_factory() as session:
        return session.scalar(select(Case).join(Invoice).where(Invoice.number == number))


def test_first_sync_copies_rows_and_opens_cases_for_overdue_invoices(
    rt: Runtime, tmp_path: Path
) -> None:
    tenant_id = make_tenant(rt, write_aging(tmp_path / "aging.csv", [OVERDUE, NOT_DUE]))

    result = sync_tenant(rt, tenant_id)

    assert (result.invoices_created, result.cases_opened) == (2, 1)
    overdue_case = case_for(rt, "INV-1")
    assert overdue_case is not None
    assert overdue_case.state == CaseState.SCHEDULED
    assert case_for(rt, "INV-2") is None  # not due yet


def test_syncing_twice_does_not_duplicate_anything(rt: Runtime, tmp_path: Path) -> None:
    tenant_id = make_tenant(rt, write_aging(tmp_path / "aging.csv", [OVERDUE, NOT_DUE]))
    sync_tenant(rt, tenant_id)
    second = sync_tenant(rt, tenant_id)

    assert (second.invoices_created, second.invoices_updated, second.cases_opened) == (0, 0, 0)
    with rt.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Invoice)) == 2
        assert session.scalar(select(func.count()).select_from(Customer)) == 1
        assert session.scalar(select(func.count()).select_from(Case)) == 1


def test_partial_payment_updates_the_amount_and_keeps_the_case_open(
    rt: Runtime, tmp_path: Path
) -> None:
    aging = tmp_path / "aging.csv"
    tenant_id = make_tenant(rt, write_aging(aging, [OVERDUE]))
    sync_tenant(rt, tenant_id)

    write_aging(aging, [{**OVERDUE, "amount_due": "400"}])
    result = sync_tenant(rt, tenant_id)

    assert result.invoices_updated == 1
    case = case_for(rt, "INV-1")
    assert case is not None and case.state == CaseState.SCHEDULED
    with rt.session_factory() as session:
        assert session.scalar(select(Invoice.amount_due)) == 40000


def test_paid_invoice_closes_its_case(rt: Runtime, tmp_path: Path) -> None:
    aging = tmp_path / "aging.csv"
    tenant_id = make_tenant(rt, write_aging(aging, [OVERDUE]))
    sync_tenant(rt, tenant_id)

    write_aging(aging, [{**OVERDUE, "amount_due": "0"}])
    result = sync_tenant(rt, tenant_id)

    assert result.cases_closed == 1
    case = case_for(rt, "INV-1")
    assert case is not None
    assert (case.state, case.closed_reason) == (CaseState.CLOSED, CloseReason.PAID)


def test_invoice_missing_from_the_source_is_removed_and_its_tasks_cancelled(
    rt: Runtime, tmp_path: Path
) -> None:
    aging = tmp_path / "aging.csv"
    tenant_id = make_tenant(rt, write_aging(aging, [OVERDUE, NOT_DUE]))
    sync_tenant(rt, tenant_id)
    with rt.session_factory.begin() as session:
        case = session.scalars(select(Case)).one()
        session.add(
            Task(
                tenant_id=tenant_id,
                case_id=case.id,
                kind=TaskKind.ESCALATION,
                summary="x",
                status=TaskStatus.OPEN,
                created_at=rt.clock.now(),
            )
        )

    write_aging(aging, [NOT_DUE])
    result = sync_tenant(rt, tenant_id)

    assert result.invoices_no_longer_open == 1
    case_after = case_for(rt, "INV-1")
    assert case_after is not None
    assert (case_after.state, case_after.closed_reason) == (CaseState.CLOSED, CloseReason.REMOVED)
    with rt.session_factory() as session:
        assert (
            session.scalar(select(Invoice.status).where(Invoice.number == "INV-1"))
            == InvoiceStatus.REMOVED
        )
        assert session.scalar(select(Task.status)) == TaskStatus.CANCELLED


def test_invoice_that_comes_back_reopens_the_same_case(rt: Runtime, tmp_path: Path) -> None:
    aging = tmp_path / "aging.csv"
    tenant_id = make_tenant(rt, write_aging(aging, [OVERDUE]))
    sync_tenant(rt, tenant_id)
    first_case = case_for(rt, "INV-1")
    write_aging(aging, [{**OVERDUE, "amount_due": "0"}])
    sync_tenant(rt, tenant_id)

    write_aging(aging, [OVERDUE])  # e.g. the payment bounced
    result = sync_tenant(rt, tenant_id)

    assert result.cases_reopened == 1
    reopened = case_for(rt, "INV-1")
    assert reopened is not None and first_case is not None
    assert reopened.id == first_case.id
    assert (reopened.state, reopened.closed_reason) == (CaseState.SCHEDULED, None)


def test_sync_keeps_core_owned_customer_fields(rt: Runtime, tmp_path: Path) -> None:
    aging = tmp_path / "aging.csv"
    tenant_id = make_tenant(rt, write_aging(aging, [OVERDUE]))
    sync_tenant(rt, tenant_id)
    with rt.session_factory.begin() as session:
        customer = session.scalars(select(Customer)).one()
        customer.paused = True
        customer.brief = "Pays late but always pays."

    write_aging(aging, [{**OVERDUE, "email": "new@example.com"}])
    sync_tenant(rt, tenant_id)

    with rt.session_factory() as session:
        customer = session.scalars(select(Customer)).one()
        assert customer.email == "new@example.com"
        assert customer.paused is True
        assert customer.brief == "Pays late but always pays."


def test_failed_sync_leaves_previous_data_in_place(rt: Runtime, tmp_path: Path) -> None:
    aging = tmp_path / "aging.csv"
    tenant_id = make_tenant(rt, write_aging(aging, [OVERDUE]))
    sync_tenant(rt, tenant_id)

    write_aging(aging, [{**OVERDUE, "amount_due": "not a number"}])
    with pytest.raises(CsvFormatError):
        sync_tenant(rt, tenant_id)

    with rt.session_factory() as session:
        assert session.scalar(select(Invoice.amount_due)) == 100000
