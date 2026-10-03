from datetime import date
from pathlib import Path

import pytest

from parley.adapters.accounting.csv import CsvAccountingAdapter, CsvFormatError
from parley.core.domain import InvoiceStatus

HEADER = (
    "Invoice Number,Customer ID,Customer Name,Email,Phone,Amount Due,Currency,Due Date,Details\n"
)


def write(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def test_reads_invoices_and_customers(tmp_path: Path) -> None:
    path = write(
        tmp_path,
        "aging.csv",
        HEADER
        + 'INV-1,C1,Asha Stores,asha@example.com,9876543210,"1,234.50",inr,2026-01-01,Order 77\n'
        + "INV-2,C1,Asha Stores,,,500,INR,2026-01-03,\n"
        + "INV-3,,Bala Traders,,,0,INR,2026-01-02,\n"
        + "\n",
    )
    adapter = CsvAccountingAdapter(path)

    open_numbers = [i.number for i in adapter.list_open_invoices()]
    assert open_numbers == ["INV-1", "INV-2"]

    inv1 = adapter.get_invoice("INV-1")
    assert inv1 is not None
    assert inv1.amount_due == 123450
    assert inv1.currency == "INR"
    assert inv1.due_date == date(2026, 1, 1)
    assert inv1.display_details == "Order 77"

    paid = adapter.get_invoice("INV-3")
    assert paid is not None and paid.status == InvoiceStatus.PAID
    assert paid.customer_external_id == "Bala Traders"  # no customer id: the name is used

    assert adapter.get_invoice("INV-404") is None

    customer = adapter.get_customer("C1")
    assert customer is not None
    assert (customer.name, customer.email, customer.phone) == (
        "Asha Stores",
        "asha@example.com",
        "9876543210",
    )


def test_accepts_excel_byte_order_mark(tmp_path: Path) -> None:
    path = tmp_path / "aging.csv"
    path.write_bytes(b"\xef\xbb\xbf" + (HEADER + "INV-1,C1,A,,,10,INR,2026-01-01,\n").encode())
    assert len(CsvAccountingAdapter(path).list_open_invoices()) == 1


def test_reports_every_bad_row(tmp_path: Path) -> None:
    path = write(
        tmp_path,
        "aging.csv",
        HEADER
        + "INV-1,C1,A,,,12.345,INR,2026-01-01,\n"
        + "INV-2,C1,A,,,10,INR,01/02/2026,\n"
        + "INV-3,C1,,,,10,INR,2026-01-01,\n"
        + "INV-4,C1,A,,,-5,INR,2026-01-01,\n"
        + "INV-4,C1,A,,,5,INR,2026-01-01,\n",
    )
    with pytest.raises(CsvFormatError) as caught:
        CsvAccountingAdapter(path).list_open_invoices()
    problems = caught.value.problems
    assert len(problems) == 4
    assert problems[0].startswith("line 2:")
    assert "decimal places" in problems[0]
    assert "YYYY-MM-DD" in problems[1]
    assert "customer_name is empty" in problems[2]
    assert "negative" in problems[3]


def test_missing_columns_are_named(tmp_path: Path) -> None:
    path = write(tmp_path, "aging.csv", "invoice_number,customer_name\nINV-1,A\n")
    with pytest.raises(CsvFormatError, match="amount_due, currency, due_date"):
        CsvAccountingAdapter(path).list_open_invoices()


def test_reads_payments(tmp_path: Path) -> None:
    invoices = write(tmp_path, "aging.csv", HEADER + "INV-1,C1,A,,,10,INR,2026-01-01,\n")
    payments = write(
        tmp_path,
        "payments.csv",
        "payment_id,customer_id,amount,currency,paid_on,reference\n"
        "P1,C1,5.00,INR,2026-01-02,UTR123\n"
        "P2,C2,7,INR,2025-12-01,\n",
    )
    adapter = CsvAccountingAdapter(invoices, payments)
    recent = adapter.list_payments_since(date(2026, 1, 1))
    assert [(p.external_id, p.amount, p.reference) for p in recent] == [("P1", 500, "UTR123")]
    assert [p.external_id for p in adapter.list_payments_since(date(2025, 1, 1), "C2")] == ["P2"]
